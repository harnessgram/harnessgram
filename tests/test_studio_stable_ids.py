"""Stable sentence IDs remain bound to their source sentence when earlier rows are dropped."""
import json

from ctb.studio import api, provider


def project_with_ids(ids):
    return {"id": "weekly-12", "notice": None, "sentences": [{"i": i} for i in ids]}


def test_marks_use_actual_sentence_ids_with_gaps_and_ignore_dropped_ids(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(provider, "project", lambda pid: project_with_ids([2, 3]))
    state = app / "state"
    state.mkdir()
    (state / "marks.json").write_text(json.dumps({"weekly-12": {
        "1": {"mark": "remove"}, "2": {"mark": "remove"}, "3": {"mark": "keep"}
    }}))

    got = api.handle({"op": "project.get", "project": "weekly-12"}, str(app))
    assert got["marks"] == {"2": {"mark": "remove"}, "3": {"mark": "keep"}}

    # The last surviving source ID is 3 even though only two sentences remain.
    saved = api.handle({"op": "marks.set", "project": "weekly-12", "i": 3, "mark": "tighten"}, str(app))
    assert saved == {"ok": True, "marks": {"2": {"mark": "remove"}, "3": {"mark": "tighten"}}}
    missing = api.handle({"op": "marks.set", "project": "weekly-12", "i": 4, "mark": "remove"}, str(app))
    assert missing == {"ok": False, "error": "bad_request"}
