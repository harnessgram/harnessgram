import base64
import json
import os
import stat

import pytest

from ctb import cli, filter_policy as policy


def b64(data):
    return base64.b64encode(data).decode()


def document(version=1, **payload_changes):
    payload = {"version": version, "locked": True, "rules": {"search": False, "stories": True}}
    payload.update(payload_changes)
    return {"payload": b64(json.dumps(payload).encode()), "signature": b64(b"s" * 64),
            "publicKey": b64(b"k" * 32)}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CTB_CONFIG_DIR", str(tmp_path / "config"))


def test_publish_atomic_and_newer(tmp_path, monkeypatch):
    doc = document()
    original = policy.config.os.replace
    calls = []

    def replace(src, dst):
        calls.append((src, dst))
        assert os.path.exists(src)
        return original(src, dst)

    monkeypatch.setattr(policy.config.os, "replace", replace)
    stored = policy.publish(doc, doc["publicKey"])
    assert policy.current() == stored
    assert all(stored[k] == v for k, v in doc.items())
    path = tmp_path / "state" / "filter-policy.json"
    assert calls and str(calls[0][1]) == str(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(policy.FilterPolicyError):
        policy.publish(doc, doc["publicKey"])
    newer = document(2)
    policy.publish(newer, newer["publicKey"])
    assert policy.current()["payload"] == newer["payload"]


@pytest.mark.parametrize("change", [
    lambda d: d.update(payload="%%%"),
    lambda d: d.update(signature=b64(b"short")),
    lambda d: d.update(publicKey=b64(b"short")),
    lambda d: d.update(extra=True),
    lambda d: d.update(payload=document(rules={"unknown": True})["payload"]),
    lambda d: d.update(payload=document(version=True)["payload"]),
    lambda d: d.update(payload=document(locked=1)["payload"]),
    lambda d: d.update(payload=document(rules={"search": 1})["payload"]),
    lambda d: d.update(payload=document(extra=True)["payload"]),
])
def test_refusals(change):
    doc = document()
    pin = doc["publicKey"]
    change(doc)
    with pytest.raises(policy.FilterPolicyError):
        policy.publish(doc, pin)
    assert policy.current() is None


@pytest.mark.parametrize("pin", ["", b64(b"x" * 32)])
def test_refuses_wrong_or_empty_pin(pin):
    with pytest.raises(policy.FilterPolicyError):
        policy.publish(document(), pin)


def test_damaged_file(tmp_path):
    path = tmp_path / "state" / "filter-policy.json"
    path.parent.mkdir()
    path.write_text('{"payload":')
    assert policy.current() is None


def test_cli(tmp_path, monkeypatch, capsys):
    doc = document()
    source = tmp_path / "policy.json"
    source.write_text(json.dumps(doc))
    monkeypatch.setattr(policy, "COMMITTEE_KEY", doc["publicKey"])
    assert cli.main(["filter-policy", "publish", str(source)]) == 0
    assert "version 1" in capsys.readouterr().out
    assert cli.main(["filter-policy", "show"]) == 0
    output = capsys.readouterr().out
    assert "version: 1" in output and "locked: true" in output and "search" in output
    assert cli.main(["filter-policy", "publish", str(source)]) == 1
    monkeypatch.setattr(policy, "COMMITTEE_KEY", "")
    monkeypatch.setattr(policy.config, "load", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert cli.main(["filter-policy", "publish", str(source)]) == 1


def test_committee_key_comes_from_config_and_is_empty_by_default(monkeypatch):
    monkeypatch.setattr(policy, "COMMITTEE_KEY", "")
    assert policy.committee_key(policy.config.Config()) == ""
    cfg = policy.config.Config()
    cfg.filter_committee_key = "abc="
    assert policy.committee_key(cfg) == "abc="
    monkeypatch.setattr(policy.config, "load", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert policy.committee_key() == ""


def test_no_committee_key_is_pinned_in_the_source():
    assert policy.COMMITTEE_KEY == ""
