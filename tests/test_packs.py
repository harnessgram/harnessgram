"""Pure pack transparency logic; fixtures only, never owner configuration."""
import json
from types import SimpleNamespace

from ctb import packs


def test_card_labels_excerpt_and_sizes_context_from_its_source(tmp_path):
    source = tmp_path / "RULES.md"
    source.write_text("Ask one clear question, then wait for its answer.\nMore source text.\n")
    card = packs.transparency_card([
        {"id": "rule:x", "source": "knowledge", "kind": "rule", "summary": "Ask clearly.",
         "text": "Ask one clear question, then wait for its answer.", "on": True,
         "source_path": str(source), "agents": ["claude", "codex"]},
        {"id": "tool:x", "source": "knowledge", "kind": "tool", "title": "Calendar",
         "network_domains": ["calendar.example"], "files_read": ["state/calendar.json"]},
        {"id": "hook:x", "source": "hook", "kind": "hook", "name": "Stop",
         "fires": "after an agent turn", "on": True, "switchable": False},
    ])
    assert card["instructions"] == {
        "count": 1, "sentences": [{"text": "Ask one clear question, then wait for its answer.",
                                      "source": str(source), "label": "excerpt"}]}
    assert [(x["id"], x["on"]) for x in card["targets"]] == [
        ("rule:x", True), ("tool:x", "unknown"), ("hook:x", True)]
    assert card["targets"][0]["label"] == "Ask clearly."
    assert card["tools"] == ["Calendar"]
    assert card["hooks"] == [{"name": "Stop", "fires": "after an agent turn"}]
    assert card["context"] == {"characters": len(source.read_text()),
                                "tokens": (len(source.read_text()) + 3) // 4}
    assert card["permissions"] == ["unknown"]
    assert card["files_written"] == ["unknown"]


def test_card_exposes_existing_switch_target_and_lock():
    card = packs.transparency_card([
        {"id": "rule:locked", "source": "knowledge", "kind": "rule", "title": "Safety",
         "text": "Keep safety enabled.", "on": True, "locked": True},
        {"id": "addon:translation", "source": "addon", "key": "translation", "kind": "addon",
         "title": "Translation", "on": False},
    ])
    assert card["targets"] == [
        {"ref": "rule:locked", "id": "rule:locked", "source": "knowledge", "key": None,
         "kind": "rule", "label": "Safety", "on": True, "locked": True, "switchable": True},
        {"ref": "addon:translation", "id": "addon:translation", "source": "addon",
         "key": "translation", "kind": "addon", "label": "Translation", "on": False,
         "locked": False, "switchable": True},
    ]


def test_unreadable_instruction_source_makes_context_unknown():
    card = packs.transparency_card([
        {"id": "doc:x", "source": "knowledge", "kind": "doc", "title": "Guide",
         "text": "The first paragraph.", "source_path": "/fixture/does-not-exist", "on": True},
    ])
    assert card["instructions"]["sentences"] == [
        {"text": "The first paragraph.", "source": "/fixture/does-not-exist", "label": "excerpt"}]
    assert card["context"] == {"characters": "unknown", "tokens": "unknown"}


def test_inventory_uses_overlay_and_native_tool_state(monkeypatch):
    monkeypatch.setattr(packs.knowledge, "read_state", lambda: {})
    monkeypatch.setattr(packs.addons, "CATALOGUE", [])
    index = {"items": [{"id": "tool:x", "kind": "tool", "title": "Calendar",
                         "agents": ["codex"], "native_off": ["codex"],
                         "source": {"path": "/fixture/config.toml"}}]}
    tool = packs._inventory(SimpleNamespace(), index)["tool:x"]
    assert tool["on"] is False and tool["connected"] is False


def test_builtins_only_keep_items_present_in_inventory(monkeypatch):
    monkeypatch.setattr(packs, "_inventory", lambda *a, **k: {"addon:translation": {}})
    monkeypatch.setattr(packs, "_custom", lambda: [])
    rows = packs.definitions(SimpleNamespace(), {"items": []})
    translation = next(x for x in rows if x["id"] == "translation")
    assert translation["items"] == ["addon:translation"]
    assert all(x == "addon:translation" for p in rows for x in p["items"])


def test_only_hooks_in_fixture_settings_enter_inventory(monkeypatch, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "/fixture/bin/ctb hook-stop"}]}]}}))
    monkeypatch.setattr(packs.install, "settings_path", lambda: settings)
    monkeypatch.setattr(packs.knowledge, "read_state", lambda: {})
    monkeypatch.setattr(packs.addons, "CATALOGUE", [])
    inv = packs._inventory(SimpleNamespace(), {"items": []})
    assert inv["hook:stop"]["fires"] == "Stop"
    assert "hook:start" not in inv
