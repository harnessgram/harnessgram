"""What the system knows: the index, switches, locked rules, undo, the overlay, privacy and the owner-only API."""
import json
import re
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctb import knowledge as K
from ctb import panel
from ctb.config import ConfigError, from_dict, state_dir
from test_cockpit_v2 import handler
from test_panel import signed

# Made-up credentials, built from pieces so no scanner mistakes this file for a leak.
FAKE_GH = "gh" + "p_" + "Zq7" * 12 + "AB"
FAKE_KEY = "sk-" + "ant-" + "Xw9" * 10
LETTER = K._hid("skill", "letter")
RULES = """# Shared rules

## 1. Talking to the owner
- Short summary at the end of every reply (team, 2026-10-24).
  It continues on this line.
- One question at a time, with the recommendation first.
  - a sub-point that belongs to the bullet above

| table | rows |
|---|---|
| are | not rules |

## 3. Safety (never relaxed)
- Secrets never in chat, commands, logs or files.
- Never send messages to other people without explicit owner approval.
- Use the deploy key: token=%s for the mirror.

```
- a code block line is not a rule
```
""" % FAKE_GH


@pytest.fixture
def world(tmp_path):
    home = Path.home()
    (home / ".agents").mkdir(parents=True, exist_ok=True)
    (home / ".agents" / "RULES.md").write_text(RULES)
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "CLAUDE.md").write_text("@~/.agents/RULES.md\n@~/.agents/SHARED_MEMORY.md\n\n# Claude additions\n- Claude only rule here.\n")
    (home / ".codex").mkdir(exist_ok=True)
    (home / ".codex" / "AGENTS.md").write_text("<!-- copy:begin -->\nold copy\n<!-- copy:end -->\n\n## Codex\n- Codex only rule.\n")
    (home / ".gemini").mkdir(exist_ok=True)
    (home / ".gemini" / "GEMINI.md").write_text("@[Rules](~/.agents/RULES.md)\n@~/.agents/SHARED_MEMORY.md\n")
    mem = home / ".claude" / "projects" / (re.sub(r"[^A-Za-z0-9]", "-", str(home)) + "-Documents") / "memory"
    mem.mkdir(parents=True)
    (home / "Documents").mkdir(exist_ok=True)
    (mem / "MEMORY.md").write_text("- [Weekly review](weekly-review.md) — every request is reviewed weekly\n")
    (mem / "weekly-review.md").write_text("---\nname: weekly-review\ndescription: Every request is reviewed in the weekly meeting. Then it moves.\n---\nBody.\n")
    (home / ".agents" / "SHARED_MEMORY.md").write_text("- [Weekly review](%s) — shared\n" % (mem / "weekly-review.md"))
    skill = home / ".codex" / "skills" / "letter"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: letter\ndescription: >-\n  Turns a few words into a formal letter.\n  Second line.\n---\n# Letter\n")
    (home / ".codex" / "config.toml").write_text('model = "x"\n[mcp_servers.drive]\ncommand = "private-cmd"\nenabled = false\n[mcp_servers."sheets"]\nargs = []\n')
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"drive": {"env": {"API_KEY": FAKE_KEY}}, "maps": {}}}))
    docs = home / "guides"
    docs.mkdir()
    (docs / "RELEASE.md").write_text("# Release guide\n\nHow releases are made. Step two follows.\n")
    return SimpleNamespace(cfg=SimpleNamespace(knowledge={"docs": ["~/guides"]}, brief={}, radio={}), home=home, mem=mem)


def items(index, kind=None):
    return [i for i in index["items"] if kind in (None, i["kind"])]


def by_title(index, part):
    return next(i for i in index["items"] if part in i["title"])


def test_scan_finds_every_kind_with_sources_dates_and_agents(world):
    ix = K.scan(world.cfg, now=1_800_000_000)
    rules = items(ix, "rule")
    titles = [r["title"] for r in rules]
    assert "Short summary at the end of every reply" in titles
    assert not any("code block" in t or "table" in t for t in titles)
    short = by_title(ix, "Short summary")
    assert short["agents"] == ["claude", "agy"]  # imported by both, never by Codex
    assert short["source"]["path"] == "~/.agents/RULES.md" and short["source"]["line"] == 4
    assert short["group"] == "Talking to the owner"
    assert "continues on this line" in short["text"]
    assert short["summary"].startswith("Short summary at the end of every reply")
    assert short["date"] == pytest.approx(time.mktime(time.strptime("2026-10-24", "%Y-%m-%d")))
    one = by_title(ix, "One question at a time")
    assert "sub-point" in one["text"]  # one bullet is one item
    assert by_title(ix, "Claude only rule")["agents"] == ["claude"]
    assert by_title(ix, "Codex only rule")["agents"] == ["codex"]
    mem = items(ix, "memory")
    assert [m["title"] for m in mem] == ["Weekly review"]
    assert mem[0]["agents"] == ["claude", "agy"] and mem[0]["group"] == ""
    assert mem[0]["summary"] == "Every request is reviewed in the weekly meeting."
    skill = items(ix, "skill")[0]
    assert skill["id"] == LETTER and skill["internal"] and skill["summary"].startswith("Turns a few words")
    tools = {t["id"]: t for t in items(ix, "tool")}
    drive = K._hid("tool", "drive")
    assert set(tools) == {drive, K._hid("tool", "sheets"), K._hid("tool", "maps")}
    assert tools[drive]["agents"] == ["claude", "codex"] and tools[drive]["native_off"] == ["codex"]
    assert [d["title"] for d in items(ix, "doc")] == ["Release guide"]
    # ids are stable across scans
    assert [i["id"] for i in K.scan(world.cfg)["items"]] == [i["id"] for i in ix["items"]]


def test_tool_settings_and_secrets_never_reach_the_index(world):
    K.scan(world.cfg)
    raw = (state_dir() / "knowledge" / "index.json").read_text()
    assert "private-cmd" not in raw and FAKE_KEY not in raw and "API_KEY" not in raw
    assert FAKE_GH not in raw and "[redacted]" in raw
    assert stat.S_IMODE((state_dir() / "knowledge" / "index.json").stat().st_mode) == 0o600


def test_locked_safety_rules_cannot_be_switched_or_suggested(world):
    ix = K.scan(world.cfg)
    locked = sorted(i["title"] for i in ix["items"] if i["locked"])
    assert locked == ["Never send messages to other people without explicit owner approval",
                      "Secrets never in chat, commands, logs or files"]
    for item in (i for i in ix["items"] if i["locked"]):
        with pytest.raises(K.Locked):
            K.toggle(world.cfg, item["id"], False)
        with pytest.raises(K.Locked):
            K.suggest(world.cfg, item["id"], "drop it")
    assert K.read_state() == {} and K.read_log() == []


def test_configured_locked_ids_and_patterns(world):
    world.cfg.knowledge.update(locked_ids=[LETTER], locked=[r"\bone question\b"])
    ix = K.scan(world.cfg)
    assert by_title(ix, "letter")["locked"] and by_title(ix, "One question")["locked"]
    with pytest.raises(K.Locked):
        K.toggle(world.cfg, LETTER, False)


def test_toggle_logs_renders_and_undo_restores(world):
    ix = K.scan(world.cfg)
    rule = by_title(ix, "Short summary")
    entry = K.toggle(world.cfg, rule["id"], False, now=100)
    assert entry["before"] is True and entry["after"] is False
    assert not K.is_on(K.read_state(), rule["id"])
    overlay = (world.home / ".agents" / "knowledge" / "off.md").read_text()
    assert rule["id"] in overlay and "Short summary" in overlay and "Rules (do not apply)" in overlay
    assert K.toggle(world.cfg, rule["id"], False)["unchanged"]
    back = K.undo(world.cfg, entry["id"], now=200)
    assert back["of"] == entry["id"] and K.is_on(K.read_state(), rule["id"])
    assert "Nothing is switched off." in (world.home / ".agents" / "knowledge" / "off.md").read_text()
    rows = K.history(now=300)
    assert [r["id"] for r in rows] == [entry["id"]] and rows[0]["undone"]
    # undoing the undo switches it off again, and the original change no longer reads as undone
    K.undo(world.cfg, back["id"], now=400)
    assert not K.is_on(K.read_state(), rule["id"]) and not K.history(now=500)[0]["undone"]
    # an undo of a change that is no longer current is refused
    K.toggle(world.cfg, rule["id"], True, now=600)
    with pytest.raises(K.Changed):
        K.undo(world.cfg, entry["id"])
    with pytest.raises(K.Unknown):
        K.undo(world.cfg, "0" * 12)
    with pytest.raises(K.KnowledgeError):
        K.undo(world.cfg, "../x")
    for name in ("state.json", "log.jsonl"):
        assert stat.S_IMODE((state_dir() / "knowledge" / name).stat().st_mode) == 0o600
    lines = (state_dir() / "knowledge" / "log.jsonl").read_text().splitlines()
    assert len(lines) == 4  # append-only: nothing is rewritten


def test_unknown_and_malformed_ids_are_refused(world):
    K.scan(world.cfg)
    with pytest.raises(K.Unknown):
        K.toggle(world.cfg, "rule:nothing.here", False)
    for bad in ("", "x", "rule:../../etc", 5):
        with pytest.raises(K.KnowledgeError):
            K.toggle(world.cfg, bad, False)
    with pytest.raises(K.KnowledgeError):
        K.toggle(world.cfg, LETTER, "off")


def test_render_never_writes_a_source_or_the_claude_home(world):
    ix = K.scan(world.cfg)
    sources = {p: (world.home / p).read_text() for p in (".agents/RULES.md", ".claude/CLAUDE.md", ".codex/AGENTS.md")}
    K.toggle(world.cfg, LETTER, False)
    for p, text in sources.items():
        assert (world.home / p).read_text() == text
    for target in ("~/.claude/knowledge-off.md", "~/.claude/CLAUDE.md", "~/.claude.json", "~/.agents/RULES.md"):
        world.cfg.knowledge["overlay"] = target
        with pytest.raises(K.Forbidden):
            K.render(world.cfg, index=ix)
    assert not (world.home / ".claude" / "knowledge-off.md").exists()
    assert sources[".claude/CLAUDE.md"] == (world.home / ".claude" / "CLAUDE.md").read_text()


def test_suggest_adds_an_owner_ledger_item_and_never_edits(world, tmp_path):
    ix = K.scan(world.cfg)
    calls = tmp_path / "calls.txt"
    tool = tmp_path / "ledger"
    tool.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" >> " + str(calls) + "\n")
    tool.chmod(0o755)
    world.cfg.brief = {"ledger_tool": str(tool)}
    rule = by_title(ix, "One question")
    before = (world.home / ".agents" / "RULES.md").read_text()
    entry = K.suggest(world.cfg, rule["id"], "Ask two questions when they are linked. password: Hq8vLp2zTr5x")
    args = calls.read_text().splitlines()
    assert args[:2] == ["env", "add"] and rule["id"] in args[2] and "two questions" in args[2]
    assert "Hq8vLp2zTr5x" not in args[2]
    assert entry["action"] == "suggest" and (world.home / ".agents" / "RULES.md").read_text() == before
    world.cfg.brief = {}
    with pytest.raises(OSError):
        K.suggest(world.cfg, rule["id"], "again")


def test_view_counts_and_history(world):
    ix = K.scan(world.cfg)
    K.toggle(world.cfg, LETTER, False)
    v = K.view(world.cfg)
    assert v["counts"]["skill"] == {"total": 1, "on": 0}
    assert v["counts"]["tool"] == {"total": 3, "on": 3}  # drive is off for Codex only, still on for Claude
    assert v["locked"] == 2 and v["week"] == 1 and v["history"][0]["item"] == LETTER
    assert v["overlay"] == "~/.agents/knowledge/off.md"
    assert len(v["items"]) == len(ix["items"])


def test_config_section_is_validated():
    assert from_dict({"knowledge": {"docs": ["~/x"], "overlay": "~/y.md"}}).knowledge["overlay"] == "~/y.md"
    for bad in ([], {"docs": "~/x"}, {"overlay": 5}, {"skills": []}):
        with pytest.raises(ConfigError):
            from_dict({"knowledge": bad})


def test_defaults_name_no_private_place():
    text = json.dumps(K.DEFAULTS)
    assert "/Users/" not in text and "/home/" not in text
    assert all(p.startswith("~/") for p in [*K.DEFAULTS["agents"].values(), *K.DEFAULTS["memory"], *K.DEFAULTS["skills"],
                                            *K.DEFAULTS["tools"].values(), K.DEFAULTS["overlay"]])


# --- the owner-only panel API ---------------------------------------------------------------------------------------

def api(body, init=None, cfg=None):
    h, replies = handler("/api/knowledge", "POST", body, signed() if init is None else init)
    if cfg is not None:
        h.server.cfg = cfg
    h.do_POST()
    status, data, _, headers = replies[0]
    return status, json.loads(data), headers


def test_api_is_owner_only(world):
    for init in ("", signed(user_id=8), signed(tamper=True), signed(auth_date=int(time.time()) - 3600),
                 "app1.sample.1.0.invalid"):
        status, data, _ = api({"action": "view"}, init, cfg=world.cfg)
        assert status == 403 and "items" not in data
    assert not (state_dir() / "knowledge" / "index.json").exists()


def test_api_view_toggle_undo_and_errors(world, monkeypatch):
    monkeypatch.setattr(panel, "build_state", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no state poll")))
    status, data, headers = api({"action": "view"}, cfg=world.cfg)
    assert status == 200 and data["counts"]["rule"]["total"] >= 4
    status, data, _ = api({"action": "toggle", "id": LETTER, "on": False}, cfg=world.cfg)
    assert status == 200 and data["entry"]["after"] is False
    status, data, _ = api({"action": "undo", "log": data["entry"]["id"]}, cfg=world.cfg)
    assert status == 200 and K.is_on(K.read_state(), LETTER)
    locked = next(i for i in K.load_index()["items"] if i["locked"])
    assert api({"action": "toggle", "id": locked["id"], "on": False}, cfg=world.cfg)[:2] == (409, {"error": "locked"})
    assert api({"action": "toggle", "id": "rule:none.none", "on": False}, cfg=world.cfg)[0] == 404
    for bad in ({"action": "toggle", "id": LETTER}, {"action": "drop"}, {"action": "view", "x": 1},
                {"action": "toggle", "id": LETTER, "on": "no"}, {"action": "suggest", "id": LETTER, "text": ""}):
        assert api(bad, cfg=world.cfg)[0] == 400
    assert api({"action": "suggest", "id": LETTER, "text": "shorter"}, cfg=world.cfg)[0] == 503  # no ledger tool


def test_the_state_poll_never_carries_the_index():
    import inspect
    assert "knowledge" not in inspect.getsource(panel.build_state)


# --- review round 1: each guard failed on the first head ------------------------------------------------------------

def test_overlay_imported_by_the_rules_is_never_indexed_and_switches_keep_working(world):
    claude = world.home / ".claude" / "CLAUDE.md"
    claude.write_text(claude.read_text() + "@~/.agents/knowledge/off.md\n")
    ix = K.scan(world.cfg)
    first, second = by_title(ix, "Short summary")["id"], by_title(ix, "One question")["id"]
    K.toggle(world.cfg, first, False)
    again = K.scan(world.cfg)  # the rules now import the overlay that holds the switched-off item
    assert [i["id"] for i in again["items"]] == [i["id"] for i in ix["items"]]
    assert not any("knowledge" in i["source"]["path"] for i in again["items"])
    K.toggle(world.cfg, second, False)
    overlay = (world.home / ".agents" / "knowledge" / "off.md").read_text()
    assert first in overlay and second in overlay


def test_a_failed_render_changes_neither_the_switch_nor_the_history(world):
    ix = K.scan(world.cfg)
    rule = by_title(ix, "Short summary")["id"]
    entry = K.toggle(world.cfg, rule, False)
    world.cfg.knowledge["overlay"] = "~/.claude/off.md"
    with pytest.raises(K.Forbidden):
        K.toggle(world.cfg, by_title(ix, "One question")["id"], False)
    with pytest.raises(K.Forbidden):
        K.undo(world.cfg, entry["id"])
    assert set(K.read_state()) == {rule} and [e["id"] for e in K.read_log()] == [entry["id"]]
    assert not (world.home / ".claude" / "off.md").exists()


def test_every_shown_field_and_id_is_masked(world):
    rules = world.home / ".agents" / "RULES.md"
    rules.write_text(rules.read_text() + "\n## Ask someone@example.com\n- Call +972 50 123 4567 or write to "
                     "someone@example.com, see https://internal.example.com/x?k=1 first.\n")
    (world.home / ".claude.json").write_text(json.dumps({"mcpServers": {"srv-" + FAKE_GH: {}}}))
    ix = K.scan(world.cfg)
    view = K.view(world.cfg)
    for blob in (json.dumps(ix["items"]), json.dumps(view)):
        for private in (FAKE_GH, "someone@example.com", "+972", "123 4567", "internal.example.com"):
            assert private not in blob
    import re
    assert all(re.fullmatch(r"(?:rule|memory|skill|tool|doc):[0-9a-f]{16}(?:-\d+)?", i["id"]) for i in ix["items"])
    call = next(i for i in ix["items"] if i["title"].startswith("Call"))
    assert "[phone]" in call["text"] and "[email]" in call["text"] and "[link]" in call["text"]
    assert call["group"] == "Ask [email]" and call["source"]["section"] == "Ask [email]"


def test_an_imports_only_file_or_a_link_to_a_source_is_never_written(world):
    only = world.home / ".agents" / "IMPORTS.md"
    only.write_text("@~/.agents/RULES.md\n")
    claude = world.home / ".claude" / "CLAUDE.md"
    claude.write_text("@~/.agents/IMPORTS.md\n" + claude.read_text())
    (world.home / "link.md").symlink_to(world.home / ".agents" / "RULES.md")
    ix = K.scan(world.cfg)
    rule = by_title(ix, "Short summary")["id"]
    rules_text = (world.home / ".agents" / "RULES.md").read_text()
    for target in ("~/.agents/IMPORTS.md", "~/link.md"):
        world.cfg.knowledge["overlay"] = target
        with pytest.raises(K.Forbidden):
            K.toggle(world.cfg, rule, False)
    assert only.read_text() == "@~/.agents/RULES.md\n" and (world.home / ".agents" / "RULES.md").read_text() == rules_text
    assert K.read_state() == {}


def test_undo_rechecks_the_lock_when_the_index_is_gone(world):
    ix = K.scan(world.cfg)
    skill = by_title(ix, "letter")["id"]
    K.toggle(world.cfg, skill, False)
    on_again = K.toggle(world.cfg, skill, True)
    world.cfg.knowledge["locked_ids"] = [skill]
    (state_dir() / "knowledge" / "index.json").unlink()
    with pytest.raises(K.Locked):
        K.undo(world.cfg, on_again["id"])
    assert K.is_on(K.read_state(), skill)
    # an item that left every source can be switched back on, never off
    world.cfg.knowledge["locked_ids"] = []
    import shutil
    shutil.rmtree(world.home / ".codex" / "skills" / "letter")
    (state_dir() / "knowledge" / "index.json").unlink()
    with pytest.raises(K.Locked):
        K.undo(world.cfg, on_again["id"])
    assert K.is_on(K.read_state(), skill)


# --- review round 2: each guard failed on the second head -----------------------------------------------------------

def test_inline_copies_are_not_part_of_stage_one(world):
    world.cfg.knowledge["inline_copies"] = [{"file": "~/.codex/AGENTS.md", "source": "~/.agents/RULES.md",
                                             "begin": "<!-- copy:begin -->", "end": "<!-- copy:end -->"}]
    before = (world.home / ".codex" / "AGENTS.md").read_text()
    K.toggle(world.cfg, by_title(K.scan(world.cfg), "Short summary")["id"], False)
    assert (world.home / ".codex" / "AGENTS.md").read_text() == before
    assert "inline_copies" not in K.DEFAULTS and not hasattr(K, "inline_drift")


@pytest.mark.parametrize("target", ["~/.agents/RULES.md", "~/rules-link.md", "~/notes.md"])
def test_the_overlay_never_replaces_a_file_it_did_not_write(world, target):
    """Pointing the overlay at a rules file (directly or through a link) or at any file it did not write is refused."""
    (world.home / "rules-link.md").symlink_to(world.home / ".agents" / "RULES.md")
    (world.home / "notes.md").write_text("my own notes\n")
    world.cfg.knowledge["overlay"] = target
    files = {p: p.read_text() for p in (world.home / ".agents" / "RULES.md", world.home / "notes.md")}
    with pytest.raises(K.Forbidden):
        ix = K.scan(world.cfg)
        K.toggle(world.cfg, by_title(ix, "Short summary")["id"], False)
    assert {p: p.read_text() for p in files} == files and K.read_state() == {}
    assert by_title(K.scan(world.cfg), "Short summary")  # the rules file is still read as a source


def test_the_overlay_file_it_wrote_is_rewritten(world):
    ix = K.scan(world.cfg)
    K.toggle(world.cfg, by_title(ix, "Short summary")["id"], False)
    K.toggle(world.cfg, by_title(ix, "One question")["id"], False)
    text = (world.home / ".agents" / "knowledge" / "off.md").read_text()
    assert text.startswith(K.OVERLAY_BEGIN) and "Short summary" in text and "One question" in text


def test_tokens_and_grouped_phone_numbers_are_masked_before_formatting_is_removed(world):
    token = "gh" + "p_" + "a1b2c3d4e5" * 4  # lowercase and digits only
    rules = world.home / ".agents" / "RULES.md"
    rules.write_text(rules.read_text() + "\n## Mirror\n- The mirror key is %s for the backup job.\n"
                     "- Phones: 050-123-4567, 03.123.4567, +972-50-123-4567 and (555) 555-0123.\n"
                     "- Keep the 2026-10-24 date, version 1.65.2 and 1000000 rows as they are.\n" % token)
    ix = K.scan(world.cfg)
    view = K.view(world.cfg)
    for blob in (json.dumps(ix), json.dumps(view)):
        assert token not in blob and token.replace("_", "") not in blob and "a1b2c3d4e5a1" not in blob
        for phone in ("050-123-4567", "03.123.4567", "+972-50-123-4567", "555-0123"):
            assert phone not in blob
    phones = by_title(ix, "Phones")
    assert phones["text"].count("[phone]") == 4
    kept = by_title(ix, "Keep the")
    assert "2026-10-24" in kept["text"] and "1.65.2" in kept["text"] and "1000000" in kept["text"]


# --- review round 3: one privacy boundary, checked as a property over every field and many forms --------------------

TOKENS = ["gh" + "p_" + "a1b2c3d4e5" * 4, "gh" + "o_" + "Z9y8X7w6V5" * 4, "sk-" + "ant-" + "k9" * 14,
          "tok_" + "x7y8z9" * 4, "AK" + "IA" + "Q7W8E9R0T1Y2U3I4", "9f8e7d6c5b4a" * 3, "xo" + "xb-" + "a1" * 6 + "-" + "b2c3" * 4]
PHONES = ["050-123-4567", "0501234567", "03.123.4567", "+972-50-123-4567", "+972 50 123 4567", "202-555-0123",
          "(202) 555-0123", "+1 (202) 555-0123", "202.555.0123", "202/555/0123"]
CONTEXTS = ["{}", "key:{}", "key={}", "({})", '"{}"', "'{}'", "/opt/{}/run", "prefix_{}", "call {} now"]


def _leaks(blob, digits_of_fields):
    found = []
    for tok in TOKENS:
        for form in (tok, tok.replace("_", ""), tok.replace("_", " "), tok.replace("-", " "), tok[5:17]):
            if form in blob:
                found.append(form)
    for phone in PHONES:
        if phone in blob or any(re.sub(r"\D", "", phone) in d for d in digits_of_fields):
            found.append(phone)
    return found


def test_no_token_or_phone_survives_in_any_field(world):
    home = world.home
    values = [c.format(v) for v in TOKENS + PHONES for c in CONTEXTS]
    rules = home / ".agents" / "RULES.md"
    rules.write_text(rules.read_text() + "".join("\n## Section %s\n- **Title %s** text %s.\n" % (v, v, v) for v in values))
    mem_root = home / ".claude" / "projects"
    for i, tok in enumerate(TOKENS):
        folder = mem_root / ("proj-" + tok) / "memory"
        folder.mkdir(parents=True)
        (folder / ("note-%s.md" % tok)).write_text("---\nname: %s\ndescription: About %s and %s.\n---\n" % (tok, tok, PHONES[i]))
        skill = home / ".codex" / "skills" / tok
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: s-%s\ndescription: Uses %s, call %s.\n---\n" % (tok, tok, PHONES[i]))
        (home / "guides" / (tok + ".md")).write_text("# Guide %s\n\nCall %s.\n" % (tok, PHONES[i]))
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"srv-" + t: {} for t in TOKENS}}))
    (home / ".codex" / "config.toml").write_text("".join('[mcp_servers."%s"]\nenabled = true\n' % t for t in TOKENS))
    ix = K.scan(world.cfg)
    view = K.view(world.cfg)
    for items in (ix["items"], view["items"]):
        fields = [str(f) for i in items for f in (i["title"], i["summary"], i["text"], i["group"],
                                                  i["source"]["path"], i["source"]["section"])]
        assert not _leaks(json.dumps(items, ensure_ascii=False), [re.sub(r"\D", "", f) for f in fields])
    assert not _leaks((state_dir() / "knowledge" / "index.json").read_text().split('"sources"')[0], [])
    # private folder names are withheld whole, never rewritten
    assert {i["group"] for i in ix["items"] if i["kind"] == "memory"} == {K.PRIVATE, ""}  # "" = the fixture Documents folder
    # dates and versions are not phone numbers
    rules.write_text("## Notes\n- Keep 2026-10-24, 1.65.2 and 150 miles.\n")
    kept = by_title(K.scan(world.cfg), "Keep")
    assert kept["title"] == "Keep 2026-10-24, 1.65.2 and 150 miles"


def test_safe_names_are_shown_as_they_are():
    assert K._safe_name("documents-notes") == "documents-notes"
    for bad in ("Documents", "a_b", "proj-" + "9f8e7d6c5b4a" * 3, "x" * 41, ""):
        assert K._safe_name(bad) == K.PRIVATE
    assert K._spath(Path.home() / ".agents" / "RULES.md") == "~/.agents/RULES.md"
    assert K._spath(Path.home() / ("proj-" + TOKENS[0]) / "a.md") == "~/…/a.md"


# --- review round 4 -------------------------------------------------------------------------------------------------

def test_a_dotted_number_with_more_than_eight_digits_is_a_phone():
    assert K.safe_text("Call +48 501.234.567 now.") == "Call [phone] now."
    assert K.safe_text("Call 501.234.567 now.") == "Call [phone] now."
    assert K.safe_text("version 1.65.2 on 2026-10-24") == "version 1.65.2 on 2026-10-24"


def test_the_index_keeps_only_hashes_of_source_paths(world):
    skill = world.home / ".codex" / "skills" / TOKENS[0]
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: plain\ndescription: Plain words.\n---\n")
    K.scan(world.cfg)
    raw = (state_dir() / "knowledge" / "index.json").read_text()
    assert TOKENS[0] not in raw and str(world.home) not in raw
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in K.load_index()["sources"])
    world.cfg.knowledge["overlay"] = str(skill / "SKILL.md")  # still refused: compared by hash
    with pytest.raises(K.Forbidden):
        K.render(world.cfg)


# --- plain-words notes ----------------------------------------------------------------------------------------------

def _note(**over):
    return dict({"title_he": "סיכום קצר בסוף", "summary_he": "בסוף כל תשובה משפט אחד פשוט.", "title_en": "Short summary",
                 "summary_en": "One plain sentence at the end of every reply.", "advice": "keep",
                 "why_he": "כך תמיד יודעים מה נעשה", "why_en": "You always know what was done"}, **over)


def _notes_file(world, data):
    path = world.home / ".agents" / "knowledge" / "notes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
    return path


def test_notes_are_read_strictly_and_bad_entries_are_ignored(world):
    ix = K.scan(world.cfg)
    good, other, third = (K.content_hash(i) for i in items(ix, "rule")[:3])
    _notes_file(world, {"items": {
        good: _note(title_he="סיכום\u202e קצר\x07", summary_he="שורה\nשנייה \u2068Claude\u2069 בסוף."),
        other: _note(title_he="א" * 61),                      # over 60
        third: _note(advice="maybe"),                          # unknown advice
        "f" * 16: _note(summary_en=5),                         # wrong type
        "NOT-A-HASH": _note(),                                 # bad key
        "e" * 16: "just text"},
        "sections": {K.section_hash("Talking to the owner"): {"title_he": "שיחה איתך", "title_en": "Talking with you"},
                     "d" * 16: {"title_he": "", "title_en": "x"}}})
    notes = K.load_notes(world.cfg)
    assert list(notes["items"]) == [good]
    kept = notes["items"][good]
    assert kept["title_he"] == "סיכום קצר" and "\u202e" not in kept["title_he"]
    assert kept["summary_he"] == "שורה שנייה \u2068Claude\u2069 בסוף."  # paired isolates stay
    assert list(notes["sections"]) == [K.section_hash("Talking to the owner")]
    assert K.clean_note(_note(why_en="\u2066open only")) == dict(_note(), why_en="open only")  # an unpaired isolate goes
    for broken in ("{not json", "[1, 2]", json.dumps({"items": []})):
        _notes_file(world, broken)
        assert K.load_notes(world.cfg) == {"items": {}, "sections": {}}


def test_the_view_carries_notes_and_a_changed_text_loses_its_note(world):
    ix = K.scan(world.cfg)
    rule = by_title(ix, "Short summary")
    _notes_file(world, {"items": {K.content_hash(rule): _note(advice="optional")},
                        "sections": {K.section_hash(rule["group"]): {"title_he": "שיחה איתך", "title_en": "Talking with you"}}})
    v = K.view(world.cfg)
    shown = next(i for i in v["items"] if i["id"] == rule["id"])
    assert shown["note"]["title_he"] == "סיכום קצר בסוף" and shown["note"]["advice"] == "optional"
    assert v["sections"] == {rule["group"]: {"title_he": "שיחה איתך", "title_en": "Talking with you"}}
    assert sum(1 for i in v["items"] if "note" in i) == 1
    rules = world.home / ".agents" / "RULES.md"
    rules.write_text(rules.read_text().replace("Short summary at the end of every reply", "Short summary at the end of each reply"))
    v = K.view(world.cfg, rescan=True)
    assert not any("note" in i for i in v["items"])  # stale hash: no note until a new one is imported
    assert any(m["kind"] == "rule" and "each reply" in m["title"] for m in K.missing_notes(world.cfg))


def test_a_locked_rule_gets_a_note_but_its_advice_is_always_keep(world):
    ix = K.scan(world.cfg)
    locked = next(i for i in items(ix, "rule") if i["locked"])
    key = K.content_hash(locked)
    counts = K.import_notes(world.cfg, [dict(_note(advice="optional"), hash=key, kind="rule")])
    assert counts["added"] == 1 and counts["locked_keep"] == 1
    assert K.load_notes(world.cfg)["items"][key]["advice"] == "keep"
    assert {k: K.load_notes(world.cfg)["items"][key][k] for k in K.LOCKED_WHY} == K.LOCKED_WHY  # never the agent's why
    _notes_file(world, {"items": {key: _note(advice="review")}})  # written by hand: the view still says keep
    shown = next(i for i in K.view(world.cfg)["items"] if i["id"] == locked["id"])
    assert shown["note"]["advice"] == "keep" and shown["note"]["why_en"] == K.LOCKED_WHY["why_en"]
    # A lock added later by the settings applies to an existing note too.
    open_rule = by_title(ix, "One question")
    _notes_file(world, {"items": {K.content_hash(open_rule): _note(advice="optional")}})
    world.cfg.knowledge["locked_ids"] = [open_rule["id"]]
    shown = next(i for i in K.view(world.cfg)["items"] if i["id"] == open_rule["id"])
    assert shown["note"]["advice"] == "keep"


def test_the_missing_export_is_masked_and_names_no_source_path(world, tmp_path):
    ix = K.scan(world.cfg)
    rows = K.missing_notes(world.cfg)
    blob = json.dumps(rows, ensure_ascii=False)
    assert FAKE_GH not in blob and FAKE_KEY not in blob and "[redacted]" in blob
    assert str(world.home) not in blob and "RULES.md" not in blob and "SKILL.md" not in blob and ".claude.json" not in blob
    assert not any(k in r for r in rows for k in ("source", "path", "line", "agents"))
    assert {r["kind"] for r in rows} == {"section", "rule", "memory", "skill", "tool", "doc"}
    assert len([r for r in rows if r["kind"] != "section"]) == len(ix["items"])
    sections = [r["section"] for r in rows if r["kind"] == "section"]
    assert "Talking to the owner" in sections and not any(s.startswith("~") for s in sections)  # no folder paths
    locked = [r for r in rows if r.get("locked")]
    assert len(locked) == 2 and all(r["kind"] == "rule" for r in locked)
    out = K.write_export(world.cfg, rows, str(tmp_path / "missing.json"))
    assert json.loads(out.read_text()) == rows and stat.S_IMODE(out.stat().st_mode) == 0o600
    with pytest.raises(K.Forbidden):
        K.write_export(world.cfg, rows, "~/.agents/RULES.md")
    with pytest.raises(K.Forbidden):
        K.write_export(world.cfg, rows, "~/.claude/missing.json")
    other = tmp_path / "settings.json"
    other.write_text('{"keep": true}')
    for bad in ("~/.claude.json", str(other), str(tmp_path / "missing.md")):
        with pytest.raises(K.Forbidden):
            K.write_export(world.cfg, rows, bad)
    assert other.read_text() == '{"keep": true}'
    assert "Short summary" in (world.home / ".agents" / "RULES.md").read_text()
    K.write_export(world.cfg, rows[:1], str(out))  # an earlier export is replaced


def test_import_merges_counts_and_writes_a_private_file(world):
    K.scan(world.cfg)
    rows = K.missing_notes(world.cfg)
    filled = []
    for r in rows:
        if r["kind"] == "section":
            filled.append(dict(r, title_he="כותרת", title_en="Heading"))
        else:
            filled.append(dict(r, **_note(advice="review")))
    filled.append(dict(_note(), hash="0" * 16, kind="rule"))     # unknown hash
    filled.append(dict(_note(title_en=""), hash=rows[-1]["hash"], kind=rows[-1]["kind"]))  # bad: empty field
    filled.append({"hash": 7})
    counts = K.import_notes(world.cfg, filled)
    assert counts == {"added": len(rows), "updated": 0, "unchanged": 0, "bad": 2, "unknown": 1, "locked_keep": 2, "pruned": 0}
    path = world.home / ".agents" / "knowledge" / "notes.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["version"] == 1
    assert K.missing_notes(world.cfg) == []
    again = K.import_notes(world.cfg, json.loads(path.read_text()))  # the notes file's own shape imports too
    assert again["unchanged"] == len(rows) and again["added"] == 0
    with pytest.raises(K.KnowledgeError):
        K.import_notes(world.cfg, "text")


def test_the_notes_file_is_never_a_source_or_under_the_claude_home(world):
    K.scan(world.cfg)
    (world.home / "list.json").write_text("[1]")
    for bad in ("~/.agents/RULES.md", "~/.claude/notes.json", "~/.agents/knowledge/notes.txt", "~/.claude.json", "~/list.json"):
        world.cfg.knowledge["notes"] = bad
        with pytest.raises(K.Forbidden):
            K.import_notes(world.cfg, [])
    assert "Short summary" in (world.home / ".agents" / "RULES.md").read_text()
    assert not (world.home / ".claude" / "notes.json").exists() and (world.home / "list.json").read_text() == "[1]"


def test_notes_command_exports_and_imports(world, tmp_path, monkeypatch, capsys):
    from ctb import cli
    monkeypatch.setattr(cli.C, "load", lambda *a, **kw: world.cfg)  # never the real bridge config
    out = tmp_path / "missing.json"
    assert cli.main(["knowledge", "notes", "--missing", "--out", str(out)]) == 0
    rows = json.loads(out.read_text())
    assert rows and "wrote %d entries" % len(rows) in capsys.readouterr().out
    first = next(r for r in rows if r["kind"] == "rule")
    filled = tmp_path / "filled.json"
    filled.write_text(json.dumps([dict(first, **_note())], ensure_ascii=False))
    assert cli.main(["knowledge", "notes", "--import", str(filled)]) == 0
    assert "added=1" in capsys.readouterr().out
    assert cli.main(["knowledge", "notes", "--missing"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == len(rows) - 1
    filled.write_text("{broken")
    assert cli.main(["knowledge", "notes", "--import", str(filled)]) == 2
    with pytest.raises(SystemExit):
        cli.main(["knowledge", "notes"])  # one of --missing or --import is required


def test_the_docs_name_every_knowledge_command_and_the_notes_fields():
    from ctb import cli
    doc = (Path(__file__).resolve().parent.parent / "docs" / "knowledge.md").read_text(encoding="utf-8")
    parser = cli._parser()
    sub = next(a for a in parser._subparsers._group_actions if a.dest == "cmd").choices["knowledge"]
    actions = next(a for a in sub._subparsers._group_actions if a.dest == "knowledge_cmd").choices
    for name in actions:
        assert "harnessgram knowledge %s" % name in doc, name
    for field in (*K.NOTE_LIMITS, "advice", *K.ADVICE, "--missing", "--import", "--out", K.DEFAULTS["notes"]):
        assert field in doc, field
    for field, limit in K.NOTE_LIMITS.items():
        assert "%d characters" % limit in doc


def test_hebrew_fields_must_be_hebrew_with_latin_only_inside_an_isolate():
    assert K.clean_note(_note(title_he="Short summary")) is None                 # no Hebrew letter
    assert K.clean_note(_note(summary_he="משפט עם Claude בפנים.")) is None       # Latin outside an isolate
    assert K.clean_note(_note(why_he="בדיקה [link] כאן")) is None                # a mask word is Latin too
    assert K.clean_note(_note(summary_he="משפט עם \u2068Claude Code\u2069 בפנים."))["summary_he"].count("\u2068") == 1
    assert K.clean_note({"title_he": "Talking", "title_en": "Talking"}, section=True) is None
    assert K.clean_note(_note(title_en="רק עברית"))  # English fields are not checked for script


def test_every_import_prunes_notes_for_text_that_is_gone(world):
    ix = K.scan(world.cfg)
    rule = by_title(ix, "Short summary")
    old = K.content_hash(rule)
    assert K.import_notes(world.cfg, [dict(_note(), hash=old, kind="rule")])["added"] == 1
    rules = world.home / ".agents" / "RULES.md"
    rules.write_text(rules.read_text().replace("every reply", "each reply"))
    ix = K.scan(world.cfg)
    new = K.content_hash(by_title(ix, "Short summary"))
    counts = K.import_notes(world.cfg, [dict(_note(), hash=new, kind="rule")])
    assert counts["added"] == 1 and counts["pruned"] == 1
    assert list(K.load_notes(world.cfg)["items"]) == [new]


def test_the_export_shows_short_labels_and_hides_home_folders(world):
    rules = world.home / ".agents" / "RULES.md"
    rules.write_text(rules.read_text() + "\n## Paths\n- Keep logs in /home/someone/logs and /Users/someone/cache for now.\n")
    K.scan(world.cfg)
    rows = K.missing_notes(world.cfg)
    blob = json.dumps(rows, ensure_ascii=False)
    assert "/Users/someone/" not in blob and "/home/someone/" not in blob and "~/logs" in blob and "~/cache" in blob
    assert K._export_text("see %s/notes now" % world.home) == "see ~/notes now"
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["skill"]["section"] == "skills" and by_kind["doc"]["section"] == "guides"


def test_only_files_this_tool_wrote_are_replaced(world, tmp_path):
    K.scan(world.cfg)
    rows = K.missing_notes(world.cfg)
    for text in ('[{"a": 1}]', "[1]", '{"items": {}}'):
        other = tmp_path / "other.json"
        other.write_text(text)
        with pytest.raises(K.Forbidden):
            K.write_export(world.cfg, rows, str(other))
        assert other.read_text() == text
    for text in ('{"items": {}}', '{"version": 1, "other": 2}', "[]"):
        _notes_file(world, text)
        with pytest.raises(K.Forbidden):
            K.import_notes(world.cfg, [])
    _notes_file(world, '{"version": 1, "items": {}, "sections": {}}')
    K.import_notes(world.cfg, [])
    K.write_export(world.cfg, [], str(tmp_path / "empty.json"))
    K.write_export(world.cfg, rows, str(tmp_path / "empty.json"))  # an empty export list is its own shape too


def test_another_spelling_of_a_protected_path_is_refused(world):
    if not (world.home / ".CLAUDE").exists():
        pytest.skip("this disk tells upper and lower case apart")
    ix = K.scan(world.cfg)
    sources = ix["sources"]
    for path in ("~/.CLAUDE/x.json", "~/.Claude/settings.json", "~/.CLAUDE.JSON", "~/.AGENTS/RULES.md", "~/.agents/rules.md"):
        assert not K.writable(K._path(path), sources), path
    with pytest.raises(K.Forbidden):
        K.write_export(world.cfg, [], "~/.CLAUDE/missing.json")
    world.cfg.knowledge["notes"] = "~/.Claude/notes.json"
    with pytest.raises(K.Forbidden):
        K.import_notes(world.cfg, [])
    assert not any(p.name in ("missing.json", "notes.json") for p in (world.home / ".claude").iterdir())
    assert K.writable(K._path("~/.agents/knowledge/notes.json"), sources)


def _enc(path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


@pytest.mark.parametrize("home_name", ["someone", "mary-jane", "Mary_Jane"])
def test_project_label_resolves_against_real_folders(tmp_path, home_name):
    home = tmp_path / "Users" / home_name
    for d in ("Documents/orot-studies", "Documents/my-project-alpha", "Projects/MyApp", "lingoline", "Documents/Web-Site/app"):
        (home / d).mkdir(parents=True)
    label = lambda p: K._project_label(_enc(p), home)
    assert label(home / "Documents" / "orot-studies") == "Documents \u00b7 orot studies"
    assert label(home / "Documents" / "my-project-alpha") == "Documents \u00b7 my project alpha"
    assert label(home / "Projects" / "MyApp") == "Projects \u00b7 MyApp"
    assert label(home / "lingoline") == "lingoline"
    assert label(home / "Documents" / "Web-Site" / "app") == "Web Site \u00b7 app"
    assert label(home / "Documents") == ""
    assert label(home) == ""
    assert label(home / "Documents" / "gone-folder") == ""  # only the part that exists is used
    assert label(home / "nothing-here") == K.PRIVATE
    assert home_name.split("-")[0].lower() not in label(home / "lingoline").lower()


def test_project_label_outside_home_or_malformed_is_private(tmp_path):
    home = tmp_path / "Users" / "someone"
    (home / "Documents").mkdir(parents=True)
    other = tmp_path / "opt" / "client" / "secretproject"
    other.mkdir(parents=True)
    for enc in (_enc(other), "", "---", "Users-someone", _enc(home) + "x-Documents", _enc(tmp_path / "Users" / "someone2")):
        assert K._project_label(enc, home) == K.PRIVATE


def test_memory_group_is_a_friendly_label_and_ids_stay_stable(world, monkeypatch):
    monkeypatch.setenv("HOME", str(world.home))
    (world.home / "orot-studies").mkdir()
    (world.home / "Documents").mkdir(exist_ok=True)
    root = world.home / ".claude" / "projects"
    for name in (_enc(world.home / "orot-studies"), _enc(world.home / "Documents")):
        (root / name / "memory").mkdir(parents=True, exist_ok=True)
        (root / name / "memory" / "n.md").write_text("---\nname: n\ndescription: A note.\n---\n")
    ix = K.scan(world.cfg)
    groups = {i["group"] for i in ix["items"] if i["kind"] == "memory"}
    assert {"orot studies", ""} <= groups
    assert str(world.home.name) not in {g for g in groups}
    assert any(i["id"] == K._hid("memory", K.PRIVATE, "n") for i in ix["items"] if i["kind"] == "memory")
