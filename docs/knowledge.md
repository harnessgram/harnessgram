# What the system knows

An owner-only Cockpit area (Settings, first group) and a `harnessgram knowledge` command that show everything your
agents read: rules, memories, skills, tools and docs. Every item is one plain sentence with its source, its date and
the agents it applies to. The owner can switch items off and on, undo any change with one tap, and send a change
request to the team. Safety rules are locked: readable, never switchable.

## How a switch takes effect

Nothing rewrites a source file. The bridge keeps three private files in its state folder
(`~/.local/state/claude-telegram-bridge/knowledge/`, mode 0600):

| File | What it holds |
|---|---|
| `index.json` | The scanned items (text only; credential-looking text masked). |
| `state.json` | Only the items that are off. A missing item is on. |
| `log.jsonl` | Append-only history: every switch, undo and suggestion. Undo appends the inverse. |

After every switch the bridge renders one overlay file, by default `~/.agents/knowledge/off.md`, that lists what is
off ("do not apply / rely on / use / call these"). Import it once from each agent's rules file, for example add the
line `@~/.agents/knowledge/off.md` to the shared rules file your agents already import. Agents read it when a
conversation starts, so a switch applies **from the next conversation**; the Cockpit says so on every item.

The overlay's first line is a marker. `render` writes it only where no file exists yet, or over a file whose first
line is that marker (one it wrote itself). It never writes under the Claude home folder (`~/.claude`,
`~/.claude.json`) or onto any file a scan reads (including a file that only imports others, and following
symlinks), and it refuses before anything changes. A scan knows the overlay by that first line, never by its path,
so a rules file that imports it never turns it into items. A switch is saved only when the overlay was written: if
rendering fails, the switch and the history stay as they were.

## Locked safety rules

A rule is locked when its title or first sentence matches one of the built-in safety patterns: secrets never in
chat, no messages to other people without approval, never weaken permissions or the sandbox, no paid services
without a yes, the owner's stop list (publication, deletion, money), one production server per project, and
child-suitable content. Add patterns with `locked` or exact ids with `locked_ids`. Locked items cannot be switched or
suggested from the Cockpit or the command line; changing one is a direct conversation with the team.

## Plain-words notes

Sources are mostly English and written for agents. The Cockpit shows each item, and each rule and memory heading, in
the page's language through a private notes file (`notes`, default `~/.agents/knowledge/notes.json`, mode 0600,
written atomically). An agent writes each note once; the bridge never calls a model itself.

| Field | What it holds | Limit |
|---|---|---|
| `title_he`, `title_en` | A short plain title | 60 characters |
| `summary_he`, `summary_en` | One plain sentence | 160 characters |
| `advice` | `keep`, `optional` or `review` | |
| `why_he`, `why_en` | One short reason for the advice | 120 characters |

A heading note holds only `title_he` and `title_en`. Notes are keyed by a content hash of the item's kind and its
masked words (`items`), or of the heading (`sections`), so when the source text changes the old note simply stops
matching and the item shows its raw text again until a new note is imported. Every field is required and checked when
read: wrong types, empty or over-long fields, an unknown advice or a malformed hash make the bridge ignore that entry,
never crash. A Hebrew field (`*_he`) needs at least one Hebrew letter, and Latin letters only inside a U+2068...U+2069
isolate (a short English name); otherwise the entry is `bad`. Control characters and bidi embeddings and overrides are
dropped; directional isolates are kept only in pairs; the text passes `safe_text` like everything else. A locked
safety rule gets a note too, but its advice is always `keep` and its reason is the fixed "Safety rule, always on",
on import and again when shown.

The loop:

```
harnessgram knowledge notes --missing --out ~/missing.json   # every item and heading with no valid note
# an agent writes notes for those entries (same list, with the note fields added)
harnessgram knowledge notes --import ~/notes-new.json        # validate and merge; prints the counts
```

The export is a JSON list. Each entry has `id`, `hash`, `kind` (`rule`, `memory`, `skill`, `tool`, `doc` or
`section`) and `section`; item entries add the masked `title`, `summary`, `text` and `locked`. It never holds a source
path, a line number or unmasked text, and any home folder in the text is shown as `~`. A skill or doc item's
`section` is only the last folder name. Skill, doc and tool headings are folder paths or agent names, so only rule and
memory headings are exported as headings. `--import` takes the same list with the note fields added, or an object shaped like the
notes file (`{"items": {hash: note}, "sections": {hash: note}}`). It prints `added`, `updated`, `unchanged`, `bad`
(invalid entries), `unknown` (hashes the current index does not have) and `locked_keep` (locked rules whose advice was
set to `keep`) and `pruned` (notes whose text changed or is gone: every import drops them, so the file stays small).
`--out` and the notes file write only a new `.json` file or one this tool wrote (an export list whose entries all have
`id` and `hash`, or a notes object with `"version": 1`), never a file a scan reads and never inside the Claude home
folder, compared by file identity so another spelling of the same path (`~/.CLAUDE` on a disk that ignores case) is
refused too.

In the Cockpit a Hebrew page shows the Hebrew title and summary, an English page the English ones. Without a note
the raw text is shown in its own element (left to right for English), never on one line with Hebrew words. Each open
item shows a small advice chip ("Best kept", "Can be switched off", "Worth a look"); the item's sheet adds the reason
and the original wording below it.

## Suggest a change

On any unlocked item the owner writes, in plain words, what to change. The bridge adds one item to the configured
ledger (`brief.ledger_tool`, project `ledger_project`, default `env`) with the item id, its source and the request.
An agent prepares the edit and brings it back for approval. Nothing is edited by the suggestion itself.

## Configuration

Everything is optional. The defaults index the common Claude Code, Codex and Gemini locations:

```json
{
  "knowledge": {
    "agents": {"claude": "~/.claude/CLAUDE.md", "codex": "~/.codex/AGENTS.md", "agy": "~/.gemini/GEMINI.md"},
    "rules": [],
    "memory": ["~/.claude/projects/*/memory"],
    "memory_index": ["MEMORY.md", "SHARED_MEMORY.md"],
    "skills": {"~/.claude/skills": ["claude"], "~/.codex/skills": ["codex"], "~/.agents/skills": ["claude", "codex", "agy"]},
    "tools": {"claude": "~/.claude.json", "codex": "~/.codex/config.toml", "agy": "~/.gemini/settings.json"},
    "docs": ["~/.codex/RELEASE_GUIDE.md", "~/guides"],
    "overlay": "~/.agents/knowledge/off.md",
    "notes": "~/.agents/knowledge/notes.json",
    "locked": [],
    "locked_ids": [],
    "ledger_project": "env"
  }
}
```

- **Rules** are the bullets of each agent's root file and of every file it imports (`@path`, or Gemini's
  `@[title](path)`), under their section headings. One top-level bullet is one item; its continuation lines and
  sub-bullets belong to it. A bullet found in several agents' files is one item that applies to all of them.
  An imported file listed in `docs` is a doc; one named in `memory_index` is a memory index.
- **Memories** are the Markdown files of the memory folders (front matter `name` and `description` when present).
  A memory linked from a memory index that an agent imports also applies to that agent.
- **Skills** are folders with a `SKILL.md`. Skills that were not reviewed outside the team show "Internal skill"
  instead of a certificate.
- **Tools** are the MCP server names only. Their settings (commands, environment, keys) are never read into the
  index. A server switched off in its own settings shows that.

## Command line

```
harnessgram knowledge scan              # read every source into the private index
harnessgram knowledge list [--kind rule]
harnessgram knowledge toggle <id> on|off
harnessgram knowledge history           # the last 30 days, with log ids
harnessgram knowledge undo <log id>
harnessgram knowledge render            # write the overlay again
harnessgram knowledge off [--json]      # what is off, for scripts and hooks
harnessgram knowledge notes --missing [--out FILE]   # items and headings with no plain-words note (masked)
harnessgram knowledge notes --import FILE            # validate and merge notes
```

## Privacy

The bridge never sends the index anywhere: no translation, no model, no network call. The only way text leaves it
is `knowledge notes --missing`, which the owner's own agents run to write notes; it carries masked text only, with no
source paths. The Cockpit reads it only
through `POST /api/knowledge`, which needs the owner's fresh Telegram login like every other owner action; the
state poll never carries it, and Cockpit apps cannot reach it. One function, `safe_text`, is the privacy boundary: it runs on the raw source text
before any formatting is removed, when an item is built and again when it is shown. It hides credentials, links,
email addresses, long tokens (20 or more letters, digits, `_` or `-` that mix letters and digits) and phone numbers
(any run of 9 to 15 digits in any grouping); dates and version numbers stay. Folder names are shown only when they
are plainly safe (lower-case letters, digits and dashes); otherwise the Cockpit says "private folder", and a path
shows "…" for any part it withholds. A memory folder is labelled by the project it belongs to: the encoded form of the real home folder is stripped as an
exact prefix (so a user name, dashes included, is never shown), the rest is matched against the real folders under home
(longest name first, so dashes inside real names are kept), and the last two folder names remain
(`Documents · orot studies`; home itself or only `Documents` shows "General"). A folder outside home is private. The label
is display-only: ids still hash the stored folder name. "Private folder" stays only when nothing safe remains. Item ids are opaque hashes of the item's place and never contain its text.

## Follow-up (not in stage 1)

Rules and memories are enforced by the overlay. Skills and tools are listed in the overlay too, but a model may still
call them. The next stage adds one Claude Code `PreToolUse` hook (matcher `Skill|mcp__.*`) that reads
`harnessgram knowledge off --json` and denies a switched-off skill or tool with a short reason. It changes the
Claude settings file, so the owner runs that one setup step himself; agents never edit the Claude home folder.
A generated copy of the rules for an agent that cannot import files is also left for a later stage.
