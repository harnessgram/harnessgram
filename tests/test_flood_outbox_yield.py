import json

from ctb import flood


def test_outbox_yields_only_fresh_budgeted_traffic(tmp_path, monkeypatch):
    monkeypatch.setattr(flood, "state_dir", lambda: tmp_path)
    (tmp_path / "reply-outbox.json").write_text(json.dumps([
        {"chat": "7", "created": 1000},
    ]))
    assert flood._replies_waiting("7", 1001)
    assert not flood._replies_waiting("8", 1001)
    assert not flood._replies_waiting("7", 1121)
    assert flood.allow_optional(7, budget=False, now=1001)


def test_bad_outbox_never_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(flood, "state_dir", lambda: tmp_path)
    path = tmp_path / "reply-outbox.json"
    for value in ("garbage", "{}"):
        path.write_text(value)
        assert not flood._replies_waiting("7", 1001)
