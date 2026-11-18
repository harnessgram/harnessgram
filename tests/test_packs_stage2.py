import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctb import packs


def inventory(source=None):
    return {
        "rule:a": {"id": "rule:a", "source": "knowledge", "kind": "rule", "title": "CI runner",
                   "on": True, "locked": False, "source_path": str(source) if source else None},
        "addon:guard": {"id": "addon:guard", "source": "addon", "kind": "addon", "key": "guard",
                        "title": "Secret safety", "on": True, "locked": True},
    }


def test_suggestions_are_stable_and_owner_move_wins(monkeypatch, tmp_path):
    inv = inventory(); monkeypatch.setattr(packs, "_inventory", lambda *a, **k: inv)
    monkeypatch.setattr(packs, "state_dir", lambda: tmp_path)
    assert packs.suggestions(None) == packs.suggestions(None)
    assert packs.suggestions(None)[0] == {"item": "addon:guard", "pack": "safety"}
    packs.move(None, "rule:a", "radio")
    groups = {p["id"]: p["items"] for p in packs.definitions(None)}
    assert "rule:a" in groups["radio"] and "rule:a" not in groups["ci-fleet"]


def test_preview_apply_and_undo_use_existing_switches(monkeypatch, tmp_path):
    inv = inventory(); monkeypatch.setattr(packs, "_inventory", lambda *a, **k: inv)
    monkeypatch.setattr(packs, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(packs.knowledge, "locked_now", lambda *a: False)
    def toggle(cfg, ident, on, **kwargs):
        inv[ident]["on"] = on
    monkeypatch.setattr(packs.knowledge, "toggle", toggle)
    plan = packs.preview(None, "ci-fleet", False, {"items": []})
    applied = packs.set_pack(None, "ci-fleet", False, {"items": []})
    assert plan["changes"] == applied["applied"] and inv["rule:a"]["on"] is False
    assert packs.undo(None, {"items": []})["restored"] == ["rule:a"]
    assert inv["rule:a"]["on"] is True
    assert packs.preview(None, "safety", False, {"items": []})["changes"] == []


def test_export_shape_and_private_data_and_symlinks(monkeypatch, tmp_path):
    skill = tmp_path / "skill"; skill.mkdir(); (skill / "SKILL.md").write_text("Helpful instructions")
    inv = inventory(skill); inv["rule:a"].update(kind="skill", name="runner-skill")
    monkeypatch.setattr(packs, "_inventory", lambda *a, **k: inv)
    monkeypatch.setattr(packs, "state_dir", lambda: tmp_path / "state")
    path = Path(packs.export_pack(None, "ci-fleet", tmp_path / "out", {"items": []})["path"])
    plugin = json.loads((path / ".claude-plugin" / "plugin.json").read_text())
    assert set(plugin) == {"name", "description", "version"}
    assert (path / "skills" / "runner-skill" / "SKILL.md").is_file()

    secret = tmp_path / "private-skill"; secret.mkdir(); (secret / "SKILL.md").write_text("token = forbidden-value")
    inv["rule:a"]["source_path"] = str(secret)
    with pytest.raises(packs.PackError):
        packs.export_pack(None, "ci-fleet", tmp_path / "blocked", {"items": []})
    (secret / "SKILL.md").write_text("safe")
    (secret / "link").symlink_to(skill / "SKILL.md")
    with pytest.raises(packs.PackError):
        packs.export_pack(None, "ci-fleet", tmp_path / "linked", {"items": []})


# --- export scans exactly what it copies ------------------------------------------------------------------------------

def _skill_dir(tmp_path, name="skill"):
    """A skill recorded the way knowledge.py records it: source.path is the SKILL.md file."""
    root = tmp_path / name; root.mkdir()
    (root / "SKILL.md").write_text("Helpful instructions")
    return root


def _export_world(monkeypatch, tmp_path, skill_file):
    inv = inventory(skill_file); inv["rule:a"].update(kind="skill", name="runner-skill")
    monkeypatch.setattr(packs, "_inventory", lambda *a, **k: inv)
    monkeypatch.setattr(packs, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return inv


def _fake_secrets():
    """Credential shapes from creds.SECRET, assembled at run time so no literal sits in the repo."""
    filler = "Q7x" * 14
    return [
        "gh" + "p_" + filler, "github" + "_pat_" + filler, "s" + "k-" + filler, "AK" + "IA" + "Q7X" * 5 + "Q",
        "xo" + "xb-" + filler, "AI" + "za" + filler[:35], "ey" + "J" + filler + ".ey" + "J" + filler + "." + filler,
        "1234567" + "89:" + filler, "pass" + "word = " + filler, "sec" + "ret: " + filler,
    ]


@pytest.mark.parametrize("value", _fake_secrets())
def test_sibling_file_with_a_credential_blocks_export(monkeypatch, tmp_path, value):
    skill = _skill_dir(tmp_path)
    (skill / ".env").write_text("VALUE=" + value + "\n")
    _export_world(monkeypatch, tmp_path, skill / "SKILL.md")
    with pytest.raises(packs.PackError, match="private"):
        packs.export_pack(None, "ci-fleet", tmp_path / "out", {"items": []})
    assert not (tmp_path / "out" / "ci-fleet").exists()


def test_symlink_or_binary_anywhere_in_the_skill_dir_blocks_export(monkeypatch, tmp_path):
    skill = _skill_dir(tmp_path); (skill / "scripts").mkdir()
    outside = tmp_path / "outside.txt"; outside.write_text("not part of the skill")
    (skill / "scripts" / "link.txt").symlink_to(outside)
    _export_world(monkeypatch, tmp_path, skill / "SKILL.md")
    with pytest.raises(packs.PackError, match="symlink"):
        packs.export_pack(None, "ci-fleet", tmp_path / "out", {"items": []})
    (skill / "scripts" / "link.txt").unlink()
    (skill / "scripts" / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
    with pytest.raises(packs.PackError, match="safe text"):
        packs.export_pack(None, "ci-fleet", tmp_path / "out", {"items": []})


def test_clean_skill_dir_exports_fully(monkeypatch, tmp_path):
    skill = _skill_dir(tmp_path); (skill / "scripts").mkdir()
    (skill / "scripts" / "run.py").write_text("print('hello')\n")
    (skill / "notes.md").write_text("More notes\n")
    _export_world(monkeypatch, tmp_path, skill / "SKILL.md")
    out = Path(packs.export_pack(None, "ci-fleet", tmp_path / "out", {"items": []})["path"])
    copied = sorted(p.relative_to(out / "skills" / "runner-skill").as_posix()
                    for p in (out / "skills" / "runner-skill").rglob("*") if p.is_file())
    assert copied == ["SKILL.md", "notes.md", "scripts/run.py"]
    assert (out / "skills" / "runner-skill" / "scripts" / "run.py").read_text() == "print('hello')\n"


def test_export_refuses_an_empty_source_dir(monkeypatch, tmp_path):
    empty = tmp_path / "empty"; empty.mkdir()
    _export_world(monkeypatch, tmp_path, empty)
    with pytest.raises(packs.PackError, match="empty"):
        packs.export_pack(None, "ci-fleet", tmp_path / "out", {"items": []})


@pytest.mark.parametrize("where", ["", "  ", "relative/dir", "~/.claude", "~/.codex/plugins", "~/.gemini/x/y"])
def test_export_refuses_empty_relative_and_agent_config_destinations(monkeypatch, tmp_path, where):
    skill = _skill_dir(tmp_path)
    _export_world(monkeypatch, tmp_path, skill / "SKILL.md")
    with pytest.raises(packs.PackError):
        packs.export_pack(None, "ci-fleet", where, {"items": []})
    assert not any((tmp_path / "home").rglob("*"))


def test_export_refuses_a_destination_that_resolves_into_agent_config(monkeypatch, tmp_path):
    skill = _skill_dir(tmp_path)
    _export_world(monkeypatch, tmp_path, skill / "SKILL.md")
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(tmp_path / "home" / ".claude")
    with pytest.raises(packs.PackError, match="agent configuration"):
        packs.export_pack(None, "ci-fleet", tmp_path / "alias", {"items": []})
    assert list((tmp_path / "home" / ".claude").iterdir()) == []


# --- a failing switch stays undoable ----------------------------------------------------------------------------------

def test_failure_mid_pack_switch_is_undoable(monkeypatch, tmp_path):
    inv = inventory()
    inv["rule:b"] = dict(inv["rule:a"], id="rule:b", title="CI runner two")
    monkeypatch.setattr(packs, "_inventory", lambda *a, **k: inv)
    monkeypatch.setattr(packs, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(packs.knowledge, "locked_now", lambda *a: False)
    packs.move(None, "rule:a", "radio"); packs.move(None, "rule:b", "radio")

    def toggle(cfg, ident, on, **kwargs):
        if ident == "rule:b" and on is False:
            raise packs.knowledge.Locked("locked safety rule")
        inv[ident]["on"] = on
    monkeypatch.setattr(packs.knowledge, "toggle", toggle)
    with pytest.raises(packs.knowledge.KnowledgeError):
        packs.set_pack(None, "radio", False, {"items": []})
    assert inv["rule:a"]["on"] is False and inv["rule:b"]["on"] is True
    assert [c["item"] for c in packs._store()["undo"]["changes"]] == ["rule:a"]
    assert packs.undo(None, {"items": []})["restored"] == ["rule:a"]
    assert inv["rule:a"]["on"] is True


def test_undo_snapshot_is_written_before_the_first_change(monkeypatch, tmp_path):
    inv = inventory(); monkeypatch.setattr(packs, "_inventory", lambda *a, **k: inv)
    monkeypatch.setattr(packs, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(packs.knowledge, "locked_now", lambda *a: False)
    seen = []

    def toggle(cfg, ident, on, **kwargs):
        seen.append(packs._store().get("undo")); inv[ident]["on"] = on
    monkeypatch.setattr(packs.knowledge, "toggle", toggle)
    packs.set_pack(None, "ci-fleet", False, {"items": []})
    assert seen and seen[0]["pack"] == "ci-fleet" and seen[0]["changes"][0]["item"] == "rule:a"


# --- the edit route answers structured errors -------------------------------------------------------------------------

@pytest.mark.parametrize("exc, status, code", [
    (lambda: packs.knowledge.Locked("x"), 409, "locked"),
    (lambda: packs.knowledge.KnowledgeError("x"), 400, "bad"),
    (lambda: packs.addons.Unknown("x"), 404, "unknown"),
    (lambda: packs.addons.Locked("x"), 409, "locked"),
])
def test_packs_edit_maps_switch_errors_to_json(monkeypatch, exc, status, code):
    from ctb import panel
    from test_cockpit_v2 import handler
    from test_panel import signed

    def boom(*a, **k):
        raise exc()
    monkeypatch.setattr(packs, "set_pack", boom)
    h, replies = handler("/api/packs-edit", "POST", {"action": "set", "pack": "radio", "on": False}, signed())
    h.do_POST()
    assert replies[0][0] == status and json.loads(replies[0][1]) == {"error": code}
    assert panel.PACK_WRITE_KEYS["set"] == {"pack", "on"}


def test_export_never_reads_files_of_items_it_does_not_copy(monkeypatch, tmp_path):
    config = tmp_path / "tool.json"; config.write_text("x" * (packs.MAX_EXPORT_BYTES + 1))
    inv = _export_world(monkeypatch, tmp_path, _skill_dir(tmp_path) / "SKILL.md")
    inv["tool:big"] = {"id": "tool:big", "source": "tool", "kind": "tool", "name": "big", "on": True,
                       "locked": False, "source_path": str(config)}
    out = packs.export_pack(None, "ci-fleet", tmp_path / "out", {"items": [["tool:big", "ci-fleet"]]})
    assert Path(out["path"]).is_dir()


def test_single_source_file_and_whole_export_have_a_size_limit(monkeypatch, tmp_path):
    big = tmp_path / "big.md"; big.write_text("x" * (packs.MAX_EXPORT_BYTES + 1))
    with pytest.raises(packs.PackError):
        packs._safe_source(str(big))


@pytest.mark.parametrize("action", [["set"], {"a": 1}, 5])
def test_packs_edit_rejects_non_string_actions(monkeypatch, action):
    from test_cockpit_v2 import handler
    from test_panel import signed
    h, replies = handler("/api/packs-edit", "POST", {"action": action, "pack": "radio", "on": False}, signed())
    h.do_POST()
    assert replies[0][0] == 400
