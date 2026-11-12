import json

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from ctb import panel_page, session_usage as u


def row(model="claude-sonnet-4-20250514", side=False, **values):
    return {"type": "assistant", "isSidechain": side, "timestamp": "2026-11-11T10:00:00Z",
            "message": {"model": model, "usage": values}}


def write(path, value):
    with path.open("ab") as f:
        f.write(json.dumps(value).encode() + b"\n")


def test_incremental_sidechain_and_partial_garbled(tmp_path):
    p = tmp_path / "sample.jsonl"
    write(p, row(input_tokens=10, output_tokens=3))
    write(p, row(side=True, cache_read_input_tokens=20))
    p.open("ab").write(b'{"unfinished"')
    first = u.scan_file(p, None)
    day = first["data"]["2026-11-11"]
    assert u.aggregate([day])["tokens"] == 33
    with p.open("ab") as f:
        f.write(b' garbage}\n')
    write(p, row(cache_creation_input_tokens=7))
    second = u.scan_file(p, first)
    assert second["offset"] == p.stat().st_size
    assert u.aggregate(second["data"].values())["tokens"] == 40
    assert u.scan_file(p, second)["data"] == second["data"]


def test_truncation_restarts(tmp_path):
    p = tmp_path / "sample.jsonl"
    write(p, row(input_tokens=123))
    first = u.scan_file(p, None)
    p.write_bytes(b'')
    write(p, row(input_tokens=1))
    assert u.aggregate(u.scan_file(p, first)["data"].values())["tokens"] == 1


def test_prices_and_unknown():
    c = dict(zip(u.FIELDS, (1_000_000,) * 4))
    assert u.price("claude-sonnet-4-20250514", c) == 22.05
    assert u.price("unknown-model", c) is None
    result = u.aggregate([{"unknown-model": c}])
    assert result["unknown_models"] and result["estimate_usd"] is None


def test_subagent_files_and_week(tmp_path, monkeypatch):
    p = tmp_path / "sample.jsonl"
    write(p, row(input_tokens=10))
    sub = tmp_path / "sample" / "subagents"
    sub.mkdir(parents=True)
    write(sub / "agent.jsonl", row(side=True, output_tokens=5))
    monkeypatch.setattr(u.status, "transcript_of", lambda s: str(p))
    now = datetime(2026, 11, 11, tzinfo=ZoneInfo("Asia/Jerusalem")).timestamp()
    result = u.collect_usage([("sample", "Example")], {}, now)
    assert result["sessions"]["sample"]["today"]["tokens"] == 15
    assert result["totals"]["week"]["tokens"] == 15
    assert result["totals"]["top"][0]["name"] == "Example"


def test_page_languages():
    for lang, text in (("he", "הערכה"), ("en", "Estimate from configured prices")):
        page = panel_page.render_page(lang, "nonce").decode()
        assert text in page
        assert 'usageCard(d.usage_totals)' in page
        assert 'usageDetails(s.usage,host)' in page


def test_snapshot_does_not_wait_for_transcript_read(monkeypatch, tmp_path):
    import threading
    gate, entered = threading.Event(), threading.Event()
    def slow(*args):
        entered.set()
        gate.wait(3)
        return {"sessions": {}, "totals": None}
    monkeypatch.setattr(u, "collect_usage", slow)
    monkeypatch.setattr(u, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(u, "_started", 0)
    monkeypatch.setattr(u, "_running", False)
    try:
        result = u.snapshot([], 0)
        assert result is not None
        assert entered.wait(1)
        assert u.snapshot([], 0) is result
    finally:
        gate.set()


def test_configured_prices_override_and_group_dated_variants():
    from ctb.config import from_dict
    cfg = from_dict({"usage_prices": {"claude-sonnet-4": [1, 2, 3, 4],
                                       "claude-opus-5-5": [10, 20, 30, 40]}})
    counts = dict(zip(u.FIELDS, (1_000_000,) * 4))
    assert u.price("claude-sonnet-4-20250514", counts, cfg.usage_prices) == 10
    assert u.price("claude-opus-5-5-20261111", counts, cfg.usage_prices) == 100
    assert u.price("claude-opus-5-50", counts, cfg.usage_prices) is None
    result = u.aggregate([{"claude-opus-5-5-20261111": counts,
                           "claude-opus-5-5-20261112": counts}], cfg.usage_prices)
    assert list(result["models"]) == ["claude-opus-5-5"]
    assert result["estimate_usd"] == 200
    assert result["unpriced_tokens"] == 0


def test_usage_price_validation():
    import pytest
    from ctb.config import ConfigError, from_dict
    for bad in ([], {"": [1, 2, 3, 4]}, {"Claude Opus": [1, 2, 3, 4]},
                {"claude-opus": [1, 2, 3]}, {"claude-opus": [True, 2, 3, 4]},
                {"claude-opus": [-1, 2, 3, 4]}, {"claude-opus": [float('nan'), 2, 3, 4]},
                {"claude-opus": [float('inf'), 2, 3, 4]}, {"claude-opus": ["1", 2, 3, 4]}):
        with pytest.raises(ConfigError, match="usage_prices"):
            from_dict({"usage_prices": bad})


def test_unpriced_tokens_in_session_and_totals(tmp_path, monkeypatch):
    p = tmp_path / "sample.jsonl"
    write(p, row(model="unknown-model-20261111", input_tokens=100, output_tokens=30))
    write(p, row(model="claude-opus-5-5-20261111", input_tokens=1000))
    monkeypatch.setattr(u.status, "transcript_of", lambda _: str(p))
    now = datetime(2026, 11, 11, tzinfo=ZoneInfo("Asia/Jerusalem")).timestamp()
    result = u.collect_usage([("sample", "Example")], {}, now, {"claude-opus-5-5": [1, 2, 3, 4]})
    for v in (result["sessions"]["sample"]["today"], result["totals"]["today"], result["totals"]["week"]):
        assert v["tokens"] == 1130 and v["unpriced_tokens"] == 130
        assert v["estimate_usd"] == .001 and v["unknown_models"]
        assert v["models"]["unknown"]["estimate_usd"] is None


def test_model_display_and_unpriced_labels():
    for lang, label in (("he", "לא הוגדר מחיר"), ("en", "no price set")):
        page = panel_page.render_page(lang, "nonce").decode()
        assert label in page
        assert 'shortUsageModel(m)' in page
        assert 'nf.format(x[1].unpriced_tokens)' in page
        assert 'el("div","lbl",iso(m))' not in page


def test_duplicate_message_ids_across_appends_and_subagents(tmp_path, monkeypatch):
    p = tmp_path / "sample.jsonl"
    first = row(input_tokens=10, output_tokens=1)
    first["message"]["id"] = "message-1"
    write(p, first)
    cache = {}
    monkeypatch.setattr(u.status, "transcript_of", lambda _: str(p))
    now = datetime(2026, 11, 11, tzinfo=ZoneInfo("Asia/Jerusalem")).timestamp()
    assert u.collect_usage([("sample", "Example")], cache, now)["totals"]["today"]["tokens"] == 11
    updated = row(input_tokens=10, output_tokens=3)
    updated["message"]["id"] = "message-1"
    write(p, updated)
    sub = tmp_path / "sample" / "subagents"
    sub.mkdir(parents=True)
    write(sub / "agent.jsonl", first)
    assert u.collect_usage([("sample", "Example")], cache, now)["totals"]["today"]["tokens"] == 13


def test_exact_model_prices():
    import pytest
    cases = {
        "claude-fable-5-2": (8, 40, .20), "claude-fable-5-1": (10, 50, .25),
        "claude-fable-5": (10, 50, 1), "claude-opus-5-5": (4, 20, .20),
        "claude-opus-5": (5, 25, .5), "claude-opus-4-8": (5, 25, .5),
        "claude-opus-4-7": (5, 25, .5), "claude-opus-4-6": (5, 25, .5),
        "claude-opus-4-5": (5, 25, .5),
        "claude-sonnet-5-5": (2, 10, .10), "claude-sonnet-5": (2, 10, .2),
        "claude-sonnet-4-6": (3, 15, .3), "claude-haiku-5-5": (.1, .5, .01),
        "claude-haiku-4-5": (1, 5, .1),
    }
    counts = dict(zip(u.FIELDS, (1_000_000,) * 4))
    for model, (input_rate, output_rate, read_rate) in cases.items():
        assert u.price(model + "-20261111", counts) == pytest.approx(input_rate * 2.25 + output_rate + read_rate)
    assert u.price("claude-opus-4-5", {"cache_creation_input_tokens": 1_000_000, u.HOURLY: 1_000_000}) == pytest.approx(10)
    assert u.price("claude-fable-5-20", counts) is None


def test_cache_write_durations_and_fallback(tmp_path):
    import pytest
    p = tmp_path / "sample.jsonl"
    write(p, row(model="claude-sonnet-4-6", cache_creation_input_tokens=99,
                 cache_creation={"ephemeral_5m_input_tokens": 1_000_000,
                                 "ephemeral_1h_input_tokens": 1_000_000}))
    write(p, row(model="claude-sonnet-4-6", cache_creation_input_tokens=1_000_000, cache_creation={}))
    result = u.aggregate(u.scan_file(p, None)["data"].values())
    assert result["tokens"] == 3_000_000
    assert result["estimate_usd"] == pytest.approx(3.75 + 6 + 3.75)


def test_private_model_is_not_in_payload_even_with_old_cache(tmp_path, monkeypatch):
    p = tmp_path / "sample.jsonl"
    private_model = "sk-ant-example secret text"
    write(p, row(model=private_model, input_tokens=5))
    monkeypatch.setattr(u.status, "transcript_of", lambda _: str(p))
    now = datetime(2026, 11, 11, tzinfo=ZoneInfo("Asia/Jerusalem")).timestamp()
    old = {str(p): {"version": 2, "inode": p.stat().st_ino, "offset": p.stat().st_size,
                    "messages": {"x": ["2026-11-11", private_model, {"input_tokens": 5}]}}}
    result = u.collect_usage([("sample", "Example")], old, now)
    assert private_model not in json.dumps(result)
    assert result["totals"]["today"]["models"]["unknown"]["input_tokens"] == 5


def test_totals_deduplicate_copied_messages_across_sessions(tmp_path, monkeypatch):
    paths = {s: tmp_path / (s + ".jsonl") for s in ("first", "second")}
    first = row(input_tokens=100)
    first["message"]["id"] = "msg-1"
    first["requestId"] = "req-1"
    copy = row(input_tokens=100)  # a resumed or forked transcript copies the message, maybe without requestId
    copy["message"]["id"] = "msg-1"
    other = row(input_tokens=7)
    other["message"]["id"] = "msg-2"
    write(paths["first"], first)
    write(paths["second"], copy)
    write(paths["second"], other)
    monkeypatch.setattr(u.status, "transcript_of", lambda session: str(paths[session]))
    now = datetime(2026, 11, 11, tzinfo=ZoneInfo("Asia/Jerusalem")).timestamp()
    cache = {}
    for _ in range(2):
        result = u.collect_usage([("first", "First"), ("second", "Second")], cache, now)
        assert result["sessions"]["first"]["today"]["tokens"] == 100
        assert result["sessions"]["second"]["today"]["tokens"] == 107
        assert result["totals"]["today"]["tokens"] == 107
        assert result["totals"]["week"]["tokens"] == 107


@pytest.mark.parametrize("model", ["claude-private-project-name", "claude-sk-ant-abc123", "Claude-opus-5-5 x"])
def test_unrecognised_model_names_never_reach_payload(tmp_path, monkeypatch, model):
    p = tmp_path / "s.jsonl"
    write(p, row(model=model, input_tokens=5))
    monkeypatch.setattr(u.status, "transcript_of", lambda _: str(p))
    now = datetime(2026, 11, 11, tzinfo=ZoneInfo("Asia/Jerusalem")).timestamp()
    result = u.collect_usage([("s", "S")], {}, now)
    assert model not in json.dumps(result)
    assert result["totals"]["today"]["models"]["unknown"]["input_tokens"] == 5


def test_known_family_names_kept():
    for m in ("claude-opus-5-5", "claude-sonnet-4-5-20250929", "claude-haiku-6"):
        assert u._MODEL.fullmatch(m)
