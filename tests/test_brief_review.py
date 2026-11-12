"""Review regressions, using the real response handler without a socket."""
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctb import blocks, brief, panel, panel_settings, work_lines
from ctb.config import Config

NOW = 1_000_000.0
SID = "a1b2c3d4" + "0" * 28
CONTROLS = "".join(map(chr, [c for c in range(32) if c != 10] + list(range(127, 160)) +
                        list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A)) + [0x200E, 0x200F, 0x061C]))
# A line break is allowed text: it is kept, and the page shows it as a line break.


def response(monkeypatch, folder, translator=None, block_rows=()):
    panel_settings.write("brief_home", True)
    monkeypatch.setattr(panel, "collect", lambda *a, **k: [])
    monkeypatch.setattr(panel, "waiting_for_owner", lambda *a, **k: ([], []))
    monkeypatch.setattr(panel.accounts, "names", lambda: [])
    monkeypatch.setattr(panel.cards, "open_cards", lambda: [])
    monkeypatch.setattr(blocks, "all_blocks", lambda now: list(block_rows))
    cfg = Config(owner_chat_id=7, brief={"ledger_dir": str(folder)})
    handler = object.__new__(panel.PanelHandler)
    handler.path = "/api/state"
    handler.headers = {panel.INIT_HEADER: "sample"}
    handler.rfile = io.BytesIO(b"{}")
    handler.server = SimpleNamespace(cfg=cfg, translator=translator, bot_token="sample", owner_id=7,
                                     youtube_view=lambda: {})
    handler._off = lambda: False
    handler._body_ok = lambda limit: True
    monkeypatch.setattr(panel, "validate_init_data", lambda *a, **k: True)
    monkeypatch.setattr(panel, "start_param_of", lambda init: "")
    replies = []
    handler._json = lambda code, data: replies.append((code, json.loads(json.dumps(data))))
    handler.do_POST()
    code, data = replies.pop()
    assert code == 200 and data["brief"] is not None
    return data


def ledger(folder, items, project="Studio"):
    (folder / (project + ".json")).write_text(json.dumps({"items": items}))


def item(**kw):
    return dict({"id": 1, "text": "Choose a style", "status": "open", "wait": "owner", "updated": NOW}, **kw)


def test_2_ledger_ids_cannot_bypass_response_secret_filter(tmp_path, monkeypatch):
    secret = "token=" + "Q" * 12
    ledger(tmp_path, [item(id=secret)])
    data = response(monkeypatch, tmp_path)
    waiting = data["brief"]["waiting"]
    assert len(waiting) == 1
    assert secret not in json.dumps(data) and "Q" * 12 not in waiting[0]["id"]
    assert waiting[0]["id"] == response(monkeypatch, tmp_path)["brief"]["waiting"][0]["id"]


def test_4_all_ledger_and_block_text_is_clean_at_response_boundary(tmp_path, monkeypatch):
    dirty = "before" + CONTROLS + "after"
    ledger(tmp_path, [item(text=dirty, notes=[dirty])], "Stu\u202edio")
    block = {"id": "1" * 16, "kind": "checklist", "line": "Block\u2066", "title": dirty,
             "session": SID, "items": [{"text": dirty, "done": False}],
             "created": NOW, "updated": NOW, "expires": NOW + 3600}

    class Translator:
        def he(self, text):
            return dirty

    data = response(monkeypatch, tmp_path, Translator(), [block])
    strings = []

    def collect(value):
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(data["brief"])
    assert not any(c in text for text in strings for c in CONTROLS)
    assert data["brief"]["waiting"][0]["text"].split() == ["before", "after"]  # separators become a space, never glue words
    assert data["brief"]["waiting"][0]["text_he"].split() == ["before", "after"]  # separators become a space, never glue words
    checklist = next(row["checklist"] for row in data["brief"]["lines"] if row["checklist"])
    assert checklist["items"][0]["text"].split() == ["before", "after"]  # separators become a space, never glue words


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), 10 ** 400])
def test_6_invalid_timestamp_drops_only_its_ledger_record(tmp_path, monkeypatch, bad):
    ledger(tmp_path, [item(id=1, updated=bad), item(id=2)])
    data = response(monkeypatch, tmp_path)
    assert len(data["brief"]["waiting"]) == 1
    assert data["brief"]["headline"]["waiting"] == 1


def test_6_age_handles_overflow():
    assert brief._age(NOW, float("inf")) is None
    assert brief._age(1e308, -1e308) is None
    assert brief._age(NOW, 10 ** 400) is None


def test_7_concurrent_seen_requests_cannot_move_marker_backwards(monkeypatch):
    lower_read, higher_written = threading.Event(), threading.Event()
    stored = {}
    original = Path.read_text

    def read(path, *a, **kw):
        if path.name != brief.SEEN_FILE:
            return original(path, *a, **kw)
        snapshot = json.dumps(stored)
        if not lower_read.is_set():
            lower_read.set()
            higher_written.wait(0.3)
        return snapshot

    def write(path, text):
        stored.clear()
        stored.update(json.loads(text))
        if stored.get("7") == 200:
            higher_written.set()

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(brief, "write_private", write)

    def request(at):
        replies = []
        handler = SimpleNamespace(_request=lambda: {"at": at}, server=SimpleNamespace(owner_id=7),
                                  _json=lambda code, data: replies.append((code, data)))
        panel.PanelHandler._brief_seen(handler)
        return replies

    with ThreadPoolExecutor(max_workers=2) as pool:
        lower = pool.submit(request, 100)
        assert lower_read.wait(2)
        higher = pool.submit(request, 200)
        assert lower.result(timeout=3)[0][0] == 200
        assert higher.result(timeout=3)[0][0] == 200
    assert stored["7"] == 200


def test_1_needs_you_retains_its_own_agent_identity(monkeypatch):
    need = {"id": "1" * 16, "kind": "needs_you", "line": "Cockpit", "title": "",
            "text": "Owner approved publication", "session": SID, "card": "2" * 16,
            "created": NOW, "updated": NOW, "expires": NOW + 3600}
    card = {"id": "2" * 16, "kind": "choice", "session": "b2c3d4e5", "age": 0, "title": "Choose", "topic": ""}
    out = brief.build(Config(), {"cards": [card]}, [], NOW, provider=work_lines.SampleProvider([]),
                      block_rows=[need], card_rows=[], events=[], seen=0)
    assert out["waiting"][0]["note_by"] == SID[:8]
