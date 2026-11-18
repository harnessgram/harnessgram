"""Curated, owner-local bundles of knowledge and HarnessGram add-ons.

Pack descriptions are deliberately small.  The transparency card is derived from
the current inventory; descriptions are never treated as security claims.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Iterable, Optional

from . import addons, creds, knowledge, install
from .config import state_dir

STORE = "packs.json"
UNKNOWN = "unknown"
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

# These contain only catalogue keys.  Missing items are omitted when the pack is
# assembled, so a built-in can never advertise a feature absent from this build.
TOPICS = (
    ("ci-fleet", "CI and fleet"), ("telegram-bridge", "Telegram bridge"),
    ("translation", "Translation"),
    ("radio", "Radio"), ("content-torah", "Content and Torah"),
    ("release-stores", "Release and stores"), ("design-ui", "Design and UI"),
    ("safety", "Safety (locked)"), ("personal-family", "Personal and family"),
    ("unsorted", "Unsorted"),
)
BUILTINS = tuple({"id": ident, "name": name, "description": "Items grouped by topic.", "items": []}
                 for ident, name in TOPICS)

TOPIC_WORDS = (
    ("safety", ("safety", "secret", "approval", "permission", "owner_only", "sensitive", "locked")),
    ("radio", ("radio", "voice", "listen", "audio", "car_mode")),
    ("telegram-bridge", ("telegram", "bridge", "reply", "topic", "translation", "compact", "conversation")),
    ("ci-fleet", ("ci", "fleet", "runner", "github", "workflow", "build")),
    ("release-stores", ("release", "store", "appstore", "publish", "version")),
    ("design-ui", ("design", "cockpit", "panel", "card", "css", "ui", "icon")),
    ("content-torah", ("torah", "content", "parasha", "lesson", "filter")),
    ("personal-family", ("family", "personal", "child", "schedule", "morning")),
)


class PackError(ValueError):
    code = "bad"


class Unknown(PackError):
    code = "unknown"


def _installed_hooks() -> dict:
    """Inventory only hook commands present in Claude's actual settings."""
    try:
        settings = json.loads(install.settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(settings, dict) or settings.get("disableAllHooks") is True:
        return {}
    found = {}
    for event, groups in (settings.get("hooks") or {}).items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            for hook in group.get("hooks", []) if isinstance(group, dict) else []:
                command = hook.get("command", "") if isinstance(hook, dict) else ""
                match = install.HOOK_RE.search(command)
                if match:
                    name = match.group(0).split()[-1][5:]
                    found["hook:" + name] = {
                        "id": "hook:" + name, "source": "hook", "kind": "hook", "name": name,
                        "fires": str(event), "on": True, "switchable": False,
                    }
    return found


def _path() -> Path:
    return state_dir() / STORE


def _store() -> dict:
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {"packs": value} if isinstance(value, list) else {}


def _write(value: dict) -> None:
    from .config import write_private
    write_private(_path(), json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _custom() -> list:
    try:
        raw = _store()
    except (OSError, ValueError):
        return []
    rows = raw.get("packs", []) if isinstance(raw, dict) else raw
    out = []
    for p in rows if isinstance(rows, list) else []:
        if (isinstance(p, dict) and ID.fullmatch(p.get("id", "")) and
                all(isinstance(p.get(k), str) for k in ("name", "description")) and
                isinstance(p.get("items"), list) and all(isinstance(x, str) for x in p["items"])):
            out.append({k: p[k] for k in ("id", "name", "description", "items")})
    return out


def _inventory(cfg, index: Optional[dict] = None) -> dict:
    index = index or knowledge.fresh_index(cfg)
    inv = {}
    for raw in index.get("items", []):
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            source = raw.get("source")
            source_path = source.get("path") if isinstance(source, dict) else None
            overlay_on = knowledge.is_on(knowledge.read_state(), raw["id"])
            native_off = set(raw.get("native_off") or [])
            agents = set(raw.get("agents") or [])
            connected = overlay_on and not (bool(native_off) and native_off >= agents)
            item = dict(raw, source="knowledge", source_path=source_path, on=connected,
                        connected=connected)
            inv[raw["id"]] = item
            # Friendly references in owner-authored JSON remain resolvable without
            # replacing the opaque knowledge id.
            if raw.get("kind") in ("skill", "tool"):
                for label in (raw.get("name"), raw.get("title")):
                    if isinstance(label, str) and label:
                        inv.setdefault(raw["kind"] + ":" + label, item)
    for a in addons.CATALOGUE:
        inv["addon:" + a.key] = {
            "id": "addon:" + a.key, "source": "addon", "key": a.key, "kind": "addon",
            "title": a.title_en, "summary": a.text_en, "agents": list(a.providers),
            "locked": a.locked, "on": addons.enabled(cfg, a.key, a.providers[0]),
        }
    inv.update(_installed_hooks())
    for key, item in list(inv.items()):
        if item.get("kind") == "tool":
            label = item.get("name") or item.get("title")
            if label:
                inv.setdefault("mcp:" + str(label), item)
    return inv


def definitions(cfg, index: Optional[dict] = None) -> list:
    inv = _inventory(cfg, index)
    custom = _custom()
    merged = {p["id"]: dict(p) for p in BUILTINS}
    merged.update({p["id"]: dict(p) for p in custom})
    renames = _store().get("renames", {})
    if isinstance(renames, dict):
        for ident, name in renames.items():
            if ident in merged and isinstance(name, str) and name.strip():
                merged[ident]["name"] = name[:100]
    for p in merged.values():
        p["items"] = []
    moves = _store().get("moves", {})
    moves = moves if isinstance(moves, dict) else {}
    for ident, item in sorted(inv.items()):
        if item.get("id") is not None and ident != item.get("id"):  # aliases are not additional members
            continue
        item = dict(item, id=item.get("id", ident))
        target = moves.get(ident)
        if target not in merged:
            target = suggest_item(item)
        merged.setdefault(target, dict(id=target, name=target, description="Owner pack.", items=[]))
        merged[target]["items"].append(ident)
    return list(merged.values())


def suggest_item(item: dict) -> str:
    """Return one stable topic from metadata only; ordering is the tie breaker."""
    if item.get("locked"):
        return "safety"
    parts = [item.get("id"), item.get("kind"), item.get("key"), item.get("title"), item.get("name"),
             item.get("summary"), item.get("source_path")]
    haystack = " ".join(str(x).lower() for x in parts if x)
    if "translation" in haystack:
        return "translation"
    for topic, words in TOPIC_WORDS:
        if any(re.search(r"(?:^|[^a-z0-9])" + re.escape(word) + r"(?:$|[^a-z0-9])", haystack)
               for word in words):
            return topic
    return "unsorted"


def suggestions(cfg, index=None) -> list:
    inv = _inventory(cfg, index)
    return [{"item": ident, "pack": suggest_item(item)} for ident, item in sorted(inv.items())
            if ident == item.get("id")]


def new_pack(ident: str, name: str) -> dict:
    if not ID.fullmatch(ident or "") or not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise PackError("invalid pack")
    data = _store()
    rows = data.get("packs", [])
    if any(p.get("id") == ident for p in BUILTINS if isinstance(p, dict)) or any(
            isinstance(p, dict) and p.get("id") == ident for p in rows):
        raise PackError("pack exists")
    rows.append({"id": ident, "name": name.strip(), "description": "Owner pack.", "items": []})
    data["packs"] = rows
    _write(data)
    return {"id": ident, "name": name.strip()}


def rename_pack(cfg, ident: str, name: str, index=None) -> dict:
    if ident not in {p["id"] for p in definitions(cfg, index)} or not isinstance(name, str) or not name.strip():
        raise Unknown("unknown pack")
    data = _store(); renames = data.get("renames", {})
    renames = renames if isinstance(renames, dict) else {}
    renames[ident] = name.strip()[:100]; data["renames"] = renames; _write(data)
    return {"id": ident, "name": renames[ident]}


def move(cfg, item: str, pack: str, index=None) -> dict:
    inv = _inventory(cfg, index)
    if item not in inv or pack not in {p["id"] for p in definitions(cfg, index)}:
        raise Unknown("unknown item or pack")
    item = inv[item]["id"]
    data = _store(); moves = data.get("moves", {})
    moves = moves if isinstance(moves, dict) else {}
    moves[item] = pack; data["moves"] = moves; _write(data)
    return {"item": item, "pack": pack}


def _pack(cfg, ident: str, index=None) -> tuple[dict, dict]:
    inv = _inventory(cfg, index)
    p = next((x for x in definitions(cfg, index) if x["id"] == ident), None)
    if p is None:
        raise Unknown("unknown pack")
    return p, inv


def _values(rows: Iterable[dict], key: str) -> list:
    vals = []
    unknown = False
    for row in rows:
        if key not in row:
            unknown = True
            continue
        value = row[key]
        vals.extend(value if isinstance(value, list) else [value])
    vals = sorted({str(x) for x in vals if x not in (None, "")})
    if unknown or not vals:
        vals.append(UNKNOWN)
    return vals


def transparency_card(items: Iterable[dict], refs: Optional[Iterable[str]] = None) -> dict:
    """Compute a card from resolved inventory rows. Missing metadata stays unknown."""
    rows = [dict(x) for x in items]
    instructions = []
    tools, hooks = [], []
    chars = 0
    context_known = True
    references = list(refs) if refs is not None else [str(x.get("id", "")) for x in rows]
    targets = []
    for ref, item in zip(references, rows):
        state = item["on"] if isinstance(item.get("on"), bool) else UNKNOWN
        targets.append({"ref": ref, "id": item.get("id", ref), "source": item.get("source", UNKNOWN),
                        "key": item.get("key"), "kind": item.get("kind", UNKNOWN),
                        "label": item.get("summary") or item.get("title") or item.get("name") or ref,
                        "on": state, "locked": bool(item.get("locked")),
                        "switchable": item.get("switchable") is not False})
        if item.get("source") == "knowledge" and item.get("kind") in ("rule", "memory", "doc", "skill"):
            sentence = item.get("text")
            if isinstance(sentence, str) and sentence.strip():
                sentence = sentence.strip()
                source_path = item.get("source_path")
                source_text = None
                if isinstance(source_path, str) and source_path:
                    try:
                        source_text = Path(source_path).expanduser().read_text(encoding="utf-8")
                    except (OSError, UnicodeError):
                        pass
                excerpt = source_text is None or len(sentence) < len(source_text.strip())
                instruction = {"text": sentence, "source": source_path or UNKNOWN}
                if excerpt:
                    instruction["label"] = "excerpt"
                instructions.append(instruction)
                if source_text is None:
                    context_known = False
                else:
                    chars += len(source_text)
        if item.get("kind") == "tool":
            tools.append(item.get("title") or item.get("name") or item.get("id"))
        if item.get("kind") == "hook" or item.get("hook"):
            hooks.append({"name": item.get("name") or item.get("hook") or item.get("id"),
                          "fires": item.get("fires", UNKNOWN)})
    return {
        "targets": targets, "instructions": {"count": len(instructions), "sentences": instructions},
        "tools": sorted(str(x) for x in tools), "hooks": hooks,
        "network_domains": _values(rows, "network_domains"),
        "files_read": _values(rows, "files_read"), "files_written": _values(rows, "files_written"),
        "permissions": _values(rows, "permissions"), "agents": _values(rows, "agents"),
        "context": ({"characters": chars, "tokens": (chars + 3) // 4}
                    if context_known else {"characters": UNKNOWN, "tokens": UNKNOWN}),
    }


def show(cfg, ident: str, index=None) -> dict:
    p, inv = _pack(cfg, ident, index)
    rows = [inv[x] for x in p["items"]]
    card = transparency_card(rows, p["items"])
    values = {x.get("on") for x in rows if isinstance(x.get("on"), bool) and not x.get("locked")}
    status = "mixed" if len(values) > 1 else ("on" if values == {True} else "off" if values == {False} else "on")
    return dict(p, count=len(rows), status=status,
                locked=sum(bool(x.get("locked")) for x in rows), items=rows,
                card=card)


def view(cfg, index=None) -> dict:
    return {"ok": True, "packs": [show(cfg, p["id"], index) for p in definitions(cfg, index)]}


def preview(cfg, ident: str, on: bool, index=None) -> dict:
    if not isinstance(on, bool):
        raise PackError("state must be on or off")
    p, inv = _pack(cfg, ident, index)
    changes, skipped = [], []
    for ref in p["items"]:
        item = inv[ref]
        locked = ident == "safety" or bool(item.get("locked"))
        if item.get("source") == "knowledge":
            locked = locked or knowledge.locked_now(cfg, item)
        if locked or item.get("switchable") is False:
            skipped.append({"item": ref, "reason": "locked"})
        elif item.get("on") is not on:
            changes.append({"item": ref, "source": item["source"], "before": bool(item.get("on")), "after": on})
    return {"ok": True, "pack": ident, "on": on, "changes": changes, "skipped": skipped}


def set_pack(cfg, ident: str, on: bool, index=None) -> dict:
    index = index or knowledge.fresh_index(cfg)
    plan = preview(cfg, ident, on, index)
    # The undo snapshot is written before anything changes, so a crash mid-way stays undoable; a failure narrows it
    # to what was really applied.
    snapshot = {"pack": ident, "changes": list(plan["changes"]), "at": time.time()}
    data = _store(); data["undo"] = snapshot; _write(data)
    applied = []
    try:
        for change in plan["changes"]:
            if change["source"] == "knowledge":
                knowledge.toggle(cfg, change["item"], on, origin="pack", index=index)
            else:
                addons.set_on(cfg, change["item"].split(":", 1)[1], on)
            applied.append(change)
    except BaseException:
        data = _store()
        if applied:
            data["undo"] = dict(snapshot, changes=applied)
        else:
            data.pop("undo", None)
        _write(data)
        raise
    return dict(plan, applied=applied)


def undo(cfg, index=None) -> dict:
    data = _store(); snap = data.get("undo")
    if not isinstance(snap, dict) or not isinstance(snap.get("changes"), list):
        raise Unknown("nothing to undo")
    index = index or knowledge.fresh_index(cfg)
    restored, skipped = [], []
    inv = _inventory(cfg, index)
    for change in reversed(snap["changes"]):
        item = inv.get(change.get("item"))
        before = change.get("before")
        if not item or not isinstance(before, bool) or item.get("locked") or (
                item.get("source") == "knowledge" and knowledge.locked_now(cfg, item)):
            skipped.append({"item": change.get("item"), "reason": "locked"}); continue
        if item["source"] == "knowledge":
            knowledge.toggle(cfg, item["id"], before, origin="pack-undo", index=index)
        else:
            addons.set_on(cfg, item["key"], before)
        restored.append(change["item"])
    data.pop("undo", None); _write(data)
    return {"ok": True, "restored": restored, "skipped": skipped}


# Credentials come from the shared detector in creds.py; personal contact details are refused on top of it.
PRIVATE = re.compile(r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+|"
                     r"(?<![A-Za-z0-9])\+?\d(?:[\s()./-]{0,3}\d){8,14}(?![A-Za-z0-9])")
MAX_EXPORT_FILES = 200
MAX_EXPORT_BYTES = 2 * 1024 * 1024
AGENT_HOMES = (".claude", ".codex", ".gemini")


def _scan(path: Path) -> str:
    """Return the file's text, or refuse a file that is not plain UTF-8 or that holds private data."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise PackError("source is not safe text")
    if creds.SECRET.search(text) or PRIVATE.search(text):
        raise PackError("source contains private data")
    return text


def _safe_source(path: str, whole_dir: bool = False) -> tuple[Path, list[tuple[str, str]]]:
    """Scan exactly what an export may use: (root, [(relative path, text)]).

    A skill is recorded by its SKILL.md file but exported as its whole folder, so with whole_dir the folder holding
    the file is walked and every file in it is scanned. Symlinks anywhere, non-text files and private data refuse."""
    root = Path(path).expanduser()
    if root.is_symlink() or not root.exists():
        raise PackError("source is missing or a symlink")
    if root.is_file() and not whole_dir:
        if root.stat().st_size > MAX_EXPORT_BYTES:
            raise PackError("source is too large")
        return root.parent, [(root.name, _scan(root))]
    if root.is_file():
        root = root.parent
        if root.is_symlink():
            raise PackError("source contains a symlink")
    files, size = [], 0
    for base, dirs, names in os.walk(root, followlinks=False):
        if any((Path(base) / d).is_symlink() for d in dirs):
            raise PackError("source contains a symlink")
        dirs[:] = sorted(dirs)
        for name in sorted(names):
            f = Path(base) / name
            if f.is_symlink() or not f.is_file():
                raise PackError("source contains a symlink or special file")
            size += f.stat().st_size
            if len(files) >= MAX_EXPORT_FILES or size > MAX_EXPORT_BYTES:
                raise PackError("source is too large")
            files.append((f.relative_to(root).as_posix(), _scan(f)))
    if not files:
        raise PackError("source is empty")
    return root, files


def _destination(raw) -> Path:
    """An absolute folder outside the agents' own configuration homes."""
    if not isinstance(raw, (str, os.PathLike)) or not str(raw).strip():
        raise PackError("export destination is empty")
    destination = Path(raw).expanduser()
    if not destination.is_absolute():
        raise PackError("export destination must be an absolute path")
    real = Path(os.path.realpath(destination))
    home = Path(os.path.realpath(Path.home()))
    for name in AGENT_HOMES:
        guarded = str(home / name).casefold()
        for candidate in (str(destination).casefold(), str(real).casefold()):
            if candidate == guarded or candidate.startswith(guarded + os.sep):
                raise PackError("export destination is inside an agent configuration folder")
    return destination


def export_pack(cfg, ident: str, destination, index=None) -> dict:
    p, inv = _pack(cfg, ident, index)
    destination = _destination(destination)
    out = destination / ident
    if out.exists():
        raise PackError("export destination exists")
    skills, total = [], 0
    for ref in p["items"]:
        item = inv[ref]; source = item.get("source_path")
        # Only skills are copied; other items export by name, so their files are never read.
        if not source or item.get("kind") != "skill":
            continue
        _, files = _safe_source(source, whole_dir=True)
        total += sum(len(text.encode("utf-8")) for _, text in files)
        if total > MAX_EXPORT_BYTES:
            raise PackError("export is too large")
        skills.append((item, files))
    destination.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".pack-", dir=destination))
    try:
        (stage / ".claude-plugin").mkdir()
        plugin = {"name": ident, "description": p["description"], "version": "1.0.0"}
        (stage / ".claude-plugin" / "plugin.json").write_text(json.dumps(plugin, indent=2) + "\n")
        for item, files in skills:
            name = re.sub(r"[^a-z0-9-]", "-", str(item.get("name") or item["id"]).lower()).strip("-")
            target = stage / "skills" / (name or "skill")
            if target.exists():
                raise PackError("two skills share a name")
            for rel, text in files:
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8")
        (stage / "hooks").mkdir()
        hooks = {"hooks": [x for x in p["items"] if x.startswith("hook:")]}
        (stage / "hooks" / "hooks.json").write_text(json.dumps(hooks, indent=2) + "\n")
        tools = [inv[x].get("name") or inv[x].get("title") for x in p["items"] if inv[x].get("kind") == "tool"]
        (stage / ".mcp.json").write_text(json.dumps({"mcpServers": {str(x): {} for x in tools}}, indent=2) + "\n")
        card = transparency_card([inv[x] for x in p["items"]], p["items"])
        (stage / "TRANSPARENCY.md").write_text("# Transparency\n\n```json\n" +
                                                     json.dumps(card, ensure_ascii=False, indent=2) + "\n```\n")
        stage.rename(out)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True); raise
    return {"ok": True, "path": str(out)}
