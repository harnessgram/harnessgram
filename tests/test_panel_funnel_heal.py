import json
import subprocess

import pytest

from ctb import heartbeat, panel_funnel, doctor
from ctb.config import Config, ConfigError, from_dict


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.setattr(heartbeat, "_FUNNEL_LAST", None)
    monkeypatch.setattr(heartbeat, "_FUNNEL_STATE", None)
    monkeypatch.setattr(panel_funnel.shutil, "which", lambda name: "/bin/tailscale")


def run_round(now=1000, **kwargs):
    # Unready config exercises the independent health check without Telegram.
    return heartbeat.run_once(Config(**kwargs), now=now)


def response(public):
    return subprocess.CompletedProcess([], 0, json.dumps({"AllowFunnel": {"host.example:10000": public}}), "")


def test_on_and_unset_do_not_restore(monkeypatch):
    calls = []
    monkeypatch.setattr(panel_funnel.subprocess, "run", lambda args, **kw: calls.append(args) or response(True))
    run_round()
    assert calls == []
    run_round(panel_funnel_port=10000)
    assert calls == [["tailscale", "serve", "status", "--json"]]


def test_off_restores_and_verifies(monkeypatch):
    calls, logs = [], []
    def run(args, **kw):
        calls.append((args, kw))
        return response(len(calls) >= 3)
    monkeypatch.setattr(panel_funnel.subprocess, "run", run)
    monkeypatch.setattr("ctb.mirror.log", logs.append)
    run_round(panel_funnel_port=10000, panel_port=9999)
    assert [c[0] for c in calls] == [["tailscale", "serve", "status", "--json"],
        ["tailscale", "funnel", "--bg", "--https=10000", "http://127.0.0.1:9999"],
        ["tailscale", "serve", "status", "--json"]]
    assert all(c[1]["timeout"] == 20 for c in calls)
    assert logs == ["cockpit funnel was off; restored"]


def test_failure_once_and_rate_limit(monkeypatch):
    calls, logs = [], []
    def run(args, **kw):
        calls.append(args)
        return response(False) if args[1] == "serve" else subprocess.CompletedProcess(args, 1, "", "sensitive")
    monkeypatch.setattr(panel_funnel.subprocess, "run", run)
    monkeypatch.setattr("ctb.mirror.log", logs.append)
    for now in (1000, 1299, 1300, 1600):
        run_round(now, panel_funnel_port=10000)
    assert len(calls) == 6
    assert logs == ["cockpit funnel restore failed (command failed)"]


def test_exception_does_not_escape(monkeypatch):
    monkeypatch.setattr(panel_funnel, "status", lambda port: 1 / 0)
    assert run_round(panel_funnel_port=10000) == 0


def test_status_unknown_never_restores(monkeypatch):
    calls = []
    monkeypatch.setattr(panel_funnel.subprocess, "run", lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0, "invalid", ""))
    run_round(panel_funnel_port=10000)
    assert len(calls) == 1


def test_config_and_doctor(monkeypatch):
    assert from_dict({}).panel_funnel_port is None
    for value in (True, 0, 65536, "10000"):
        with pytest.raises(ConfigError):
            from_dict({"panel_funnel_port": value})
    monkeypatch.setattr(panel_funnel, "status", lambda port: (True, ""))
    monkeypatch.setattr(doctor, "check_config", lambda: ([], Config(panel_funnel_port=10000)))
    for name in ("check_tmux", "check_claude", "check_listener", "check_hooks", "check_running_code"):
        monkeypatch.setattr(doctor, name, lambda: [])
    for name in ("check_local_api", "check_extras", "check_service_path", "check_macos_privacy"):
        monkeypatch.setattr(doctor, name, lambda cfg: [])
    assert ("ok", "Cockpit Funnel is public") in doctor.collect(offline=True)


def test_other_port_does_not_count_and_failed_readback(monkeypatch):
    calls, logs = [], []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({"AllowFunnel": {"host:443": True}}), "")
    monkeypatch.setattr(panel_funnel.subprocess, "run", run)
    monkeypatch.setattr("ctb.mirror.log", logs.append)
    run_round(panel_funnel_port=10000)
    assert len(calls) == 3
    assert logs == ["cockpit funnel restore failed (still off)"]
