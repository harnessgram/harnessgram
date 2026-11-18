from ctb import apns, device_tokens
from ctb.config import locked_map, read_map


def _store_activity(tmp_path, device):
    with locked_map(tmp_path / apns.TOKEN_FILE) as data:
        data[device + ":sample-card"] = {
            "device": device, "card_id": "sample-card", "token": "test-token", "env": "sandbox"
        }


def test_revoke_drops_live_activity_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    paired = device_tokens.pair(now=100)
    _store_activity(tmp_path, paired["id"])
    assert device_tokens.revoke(paired["id"])
    assert read_map(tmp_path / apns.TOKEN_FILE) == {}


def test_revoke_all_drops_every_live_activity_row(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(device_tokens.time, "time", lambda: 102)
    devices = [device_tokens.pair(now=100 + offset)["id"] for offset in range(2)]
    for device in devices:
        _store_activity(tmp_path, device)
    assert device_tokens.revoke_all() == 2
    assert read_map(tmp_path / apns.TOKEN_FILE) == {}


def test_successful_revoke_ignores_live_activity_store_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    paired = device_tokens.pair(now=100)
    monkeypatch.setattr(apns, "drop_device", lambda ident: (_ for _ in ()).throw(OSError("read only")))
    assert device_tokens.revoke(paired["id"])
    assert device_tokens.listing(now=101) == []
