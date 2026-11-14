"""External ledger validation, refresh visibility and the existing translation path."""
import json

from ctb import owner_requests as requests, panel, panel_text
from ctb.config import Config

NOW = 1_800_000_000


def item(ident=1, **extra):
    return dict(id=ident, text="Review the new screen", status="open", wait="team",
                created=NOW - 100, updated=NOW - 50, notes=[], **extra)


def put(folder, project, rows):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (project + ".json")).write_text(json.dumps({"next": 100, "items": rows}))


def test_shape_order_notes_and_recent_done(tmp_path):
    team, owner, done, expired = (item(i) for i in range(1, 5))
    owner.update(wait="owner", notes=["First step", {"at": NOW - 10, "text": "Latest\nstep"}, None, {}])
    done.update(status="done", updated=NOW - requests.DONE_DAYS)
    expired.update(status="done", updated=NOW - requests.DONE_DAYS - 1)
    put(tmp_path, "alpha", [team, done, expired, owner])
    put(tmp_path, "beta", [item()])
    rows = requests.read(NOW, tmp_path)
    assert [(r["project"], r["id"]) for r in rows] == [("alpha", 2), ("alpha", 1), ("alpha", 3), ("beta", 1)]
    assert rows[0]["age"] == 100
    assert rows[0]["note"] == "Latest step"
    assert rows[0]["steps"] == [{"text": "First step"}, {"text": "Latest step"}]


def test_malformed_size_bound_symlinks_and_item_validation(tmp_path, monkeypatch):
    put(tmp_path, "valid", [None, {}, item(), item(), dict(item(2), updated=float("nan")),
                            dict(item(3), id=True), dict(item(4), text=[]), dict(item(5), status="other")])
    (tmp_path / "broken.json").write_text("{")
    (tmp_path / "shape.json").write_text("[]")
    (tmp_path / "encoding.json").write_bytes(b"\xff")
    (tmp_path / "large.json").write_text(" " * 2000)
    (tmp_path / "link.json").symlink_to(tmp_path / "valid.json")
    monkeypatch.setattr(requests, "FILE_BYTES", 1900)
    assert [(r["project"], r["id"]) for r in requests.read(NOW, tmp_path)] == [("valid", 1)]
    assert requests.read(NOW, tmp_path / "missing") == []


def test_hard_read_bound_even_when_size_changes(tmp_path, monkeypatch):
    put(tmp_path, "large", [dict(item(), text="a" * 200)])
    monkeypatch.setattr(requests, "FILE_BYTES", 32)
    original = requests.os.fstat
    class Small:
        st_size = 0
        st_mode = 0o100600
    monkeypatch.setattr(requests.os, "fstat", lambda fd: Small())
    assert requests.read(NOW, tmp_path) == []
    monkeypatch.setattr(requests.os, "fstat", original)


def test_state_reads_new_rows_on_next_poll_and_redacts_before_translation(monkeypatch, tmp_path):
    folder = tmp_path / "requests"
    put(folder, "demo", [item()])
    cfg = Config(brief={"ledger_dir": str(folder)})
    assert len(panel.build_state(cfg, now=NOW, light=True)["requests"]) == 1
    put(folder, "demo", [item(), dict(item(2), text="Request two")])
    assert len(panel.build_state(cfg, now=NOW, light=True)["requests"]) == 2
    monkeypatch.setattr(requests, "redact_channel", lambda text: text.replace("private-value", "[hidden]"))
    put(folder, "demo", [dict(item(), text="Review private-value", notes=["Step private-value"])])
    rows = requests.read(NOW, folder)
    assert "private-value" not in json.dumps(rows)


def test_ledger_location_reuses_existing_configuration():
    from pathlib import Path
    assert requests.ledger_dir(Config()) is None
    assert requests.ledger_dir(Config(radio={"ledger_dir": "~/requests"})) == Path.home() / "requests"
    assert requests.ledger_dir(Config(brief={"ledger_dir": "/brief"}, radio={"ledger_dir": "/radio"})) == Path("/brief")


def test_translation_cache_key_is_project_id_updated_and_retryable(monkeypatch):
    tr = panel_text.Translator(Config())
    calls = []
    monkeypatch.setattr(tr, "he", lambda text: calls.append(text) or "בדיקת המסך")
    assert tr.he_item("a", 1, NOW, "Review screen") == "בדיקת המסך"
    assert tr.he_item("a", 1, NOW, "Review screen") == "בדיקת המסך"
    assert len(calls) == 1
    tr.he_item("b", 1, NOW, "Review screen")
    tr.he_item("a", 1, NOW + 1, "Review screen")
    assert len(calls) == 3
    monkeypatch.setattr(tr, "he", lambda text: None)
    assert tr.he_item("a", 2, NOW, "New title") is None
    monkeypatch.setattr(tr, "he", lambda text: "כותרת חדשה")
    assert tr.he_item("a", 2, NOW, "New title") == "כותרת חדשה"


def test_translation_fallback_and_pending_do_not_hide_rows(monkeypatch):
    tr = panel_text.Translator(Config())
    rows = [dict(item(), project="demo", title="Review screen", note="", steps=[])]
    monkeypatch.setattr(tr, "he", lambda text: None if text else "")
    pending = []
    requests.translate(rows, tr, pending)
    assert rows[0]["title"] == "Review screen" and rows[0]["title_he"] is None
    assert pending == ["request"]
    monkeypatch.setattr(tr, "he", lambda text: "בדיקת המסך" if text else "")
    requests.translate(rows, tr, [])
    assert rows[0]["title_he"] == "בדיקת המסך"


def test_titles_are_queued_before_note_histories(monkeypatch):
    tr = panel_text.Translator(Config())
    seen = []
    monkeypatch.setattr(tr, "he", lambda text: seen.append(text) or None)
    rows = [dict(project="demo", id=n, updated=NOW, title=f"Title {n}", note="A note",
                 steps=[{"text": "A step"}]) for n in (1, 2)]
    requests.translate(rows, tr, [])
    assert seen[:2] == ["Title 1", "Title 2"]
