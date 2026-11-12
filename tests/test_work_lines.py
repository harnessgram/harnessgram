"""Work-lines providers for the Morning Brief: the owner ledger (a folder or a command, read-only and bounded), the
sample rows, and the configuration that picks one. All projects and texts are invented."""
import json
import sys
from pathlib import Path

from ctb import work_lines as W
from ctb.config import Config


def ledger(folder: Path, name: str, items) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps({"next": 9, "items": items}), encoding="utf-8")


def test_the_ledger_folder_gives_checked_rows(tmp_path):
    ledger(tmp_path, "Studio", [
        {"id": 1, "text": "Build the filters board", "status": "open", "wait": "", "updated": 100.0, "notes": ["a", "b"]},
        {"id": 2, "text": "Pick a style", "status": "open", "wait": "owner", "created": 50.0},
        {"id": 3, "text": "Old", "status": "dropped", "updated": 1.0},
        {"id": 4, "text": "", "status": "open"}, "junk", {"id": 5, "status": "open"},
        {"id": 6, "text": "x" * 400, "status": "done", "updated": 7.0}])
    (tmp_path / "broken.json").write_text("{nope")
    rows = W.LedgerProvider(folder=tmp_path).items(0)
    assert [r["id"] for r in rows] == ["1", "2", "6"]
    assert rows[0] == {"line": "studio", "name": "Studio", "id": "1", "text": "Build the filters board", "status": "open",
                       "wait": "", "updated": 100.0, "note": "b"}
    assert rows[1]["wait"] == "owner" and rows[1]["updated"] == 50.0
    assert len(rows[2]["text"]) == W.TEXT_CHARS and rows[2]["text"].endswith("…")


def test_a_missing_folder_says_so_and_gives_nothing(tmp_path):
    p = W.LedgerProvider(folder=tmp_path / "none")
    assert p.items(0) == [] and p.ok is False


def test_the_ledger_command_is_read_once_per_window(tmp_path):
    counter = tmp_path / "n"
    code = ("import json,pathlib;p=pathlib.Path(%r);p.write_text(str(int(p.read_text() or 0)+1) if p.exists() else '1');"
            "print(json.dumps({'radio': {'items': [{'id': 1, 'text': 'Fix the noise', 'status': 'open', 'updated': 5}]}}))"
            % str(counter))
    p = W.LedgerProvider(command=[sys.executable, "-c", code])
    assert [r["line"] for r in p.items(0)] == ["radio"] and p.ok
    p.items(0)
    assert counter.read_text() == "1"  # the second read came from the short cache


def test_a_failing_command_gives_nothing():
    p = W.LedgerProvider(command=[sys.executable, "-c", "import sys; print('{bad'); sys.exit(0)"])
    assert p.items(0) == [] and p.ok is False
    q = W.LedgerProvider(command=["/nonexistent/ledger-tool"])
    assert q.items(0) == [] and q.ok is False


def test_the_sample_rows_are_relative_to_now():
    rows = W.SampleProvider().items(10000.0)
    assert rows and all(r["updated"] < 10000.0 for r in rows) and {r["status"] for r in rows} == {"open", "done"}


def test_the_configuration_picks_the_source(tmp_path):
    assert W.from_config(Config()) is None
    assert isinstance(W.from_config(Config(brief={"lines": "sample"})), W.SampleProvider)
    p = W.from_config(Config(brief={"ledger_dir": str(tmp_path)}))
    assert isinstance(p, W.LedgerProvider) and p.folder == tmp_path
    assert W.from_config(Config(radio={"ledger_dir": str(tmp_path)})).folder == tmp_path  # the radio's, as a fallback
    c = W.from_config(Config(brief={"ledger_command": ["ledger", "list", "--json"]}))
    assert c.command == ["ledger", "list", "--json"]
    assert W.from_config(Config(brief={"ledger_command": "ledger list"})) is None  # a string is not an argv
