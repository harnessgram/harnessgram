"""Paid speech stops on damaged state, unknown outcomes and provider overruns."""
import json
import time
from concurrent.futures import ProcessPoolExecutor

import pytest

from ctb import radio_voice as V


def _reserve(path):
    return V.Usage(path).reserve(1, 60)


@pytest.mark.parametrize("content", [
    "{not json", '{}', '{"days": null}', '{"days":{"2026-11-13":"5"}}',
    '{"days":{"2026-11-13":-1}}', '{"days":{"2026-11-13":NaN}}',
    '{"days":{},"blocked":"false"}', '{"days":{},"pending":null}',
    '{"days":{},"unexpected":1}', '{"date":"2026-02-30","seconds":1}',
])
def test_damaged_ledger_blocks_and_is_not_overwritten(tmp_path, content):
    path = tmp_path / "tts-usage.json"
    path.write_text(content)
    usage = V.Usage(path)
    assert usage.reserve(10, 60) is None
    assert usage.readable_today() is None
    usage.add(5)
    assert path.read_text() == content


def test_damaged_legacy_room_blocks_import_and_preserves_shared(tmp_path):
    shared = tmp_path / 'tts-usage.json'
    original = '{"days":{}}'
    shared.write_text(original)
    legacy = tmp_path / 'room' / 'tts-usage.json'
    legacy.parent.mkdir()
    legacy.write_text('{"days":null}')
    usage = V.paid_usage(tmp_path)
    assert usage.readable_today() is None
    assert usage.reserve(1, 60) is None
    usage.add(1)
    assert shared.read_text() == original


def test_overrun_blocks_across_restarts(tmp_path):
    path = tmp_path / 'tts-usage.json'
    usage = V.Usage(path)
    reservation = usage.reserve(10, 60)
    usage.settle(reservation, reservation[0] + 1)
    assert json.loads(path.read_text())['blocked'] is True
    assert V.Usage(path).reserve(1, 60) is None


def test_failed_settlement_leaves_pending_and_blocks_restart(tmp_path, monkeypatch):
    path = tmp_path / 'tts-usage.json'
    usage = V.Usage(path)
    reservation = usage.reserve(10, 60)
    monkeypatch.setattr(usage, '_write', lambda data: (_ for _ in ()).throw(OSError('failed')))
    with pytest.raises(OSError):
        usage.settle(reservation, reservation[0] + 1)
    assert V.Usage(path).reserve(1, 60) is None


def test_unknown_result_blocks_across_midnight(tmp_path):
    usage = V.Usage(tmp_path / 'tts-usage.json')
    now = time.mktime((2026, 11, 12, 12, 0, 0, 0, 0, -1))
    reservation = usage.reserve(1, 60, now=now)
    usage.settle(reservation, None)
    assert V.Usage(usage.path).reserve(1, 60, now=now) is None
    assert V.Usage(usage.path).reserve(1, 60, now=now + 86400) is None


def test_only_one_process_can_reserve(tmp_path):
    path = tmp_path / 'tts-usage.json'
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_reserve, [path] * 8))
    assert sum(r is not None for r in results) == 1
