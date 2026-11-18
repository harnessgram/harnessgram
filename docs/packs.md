# Agent packs

Agent packs are curated, owner-local views of capabilities already installed in HarnessGram. A pack can contain knowledge index IDs, add-on keys, hooks, skills, and MCP tools or servers. **Stage 1 is transparency only:** packs cannot turn anything on or off. There is no catalogue, download, publishing, or sharing workflow.

## Storage and discovery

HarnessGram supplies a small set of built-in packs and reads optional owner definitions from `packs.json` in the private bridge state directory. The file is either a JSON array or `{ "packs": [...] }`; each entry has `id`, `name`, `description`, and `items`. References use an existing opaque knowledge ID or a qualified reference such as `addon:translation`, `skill:name`, or `tool:name`.

Built-in definitions are intersected with the live knowledge scan and add-on catalogue, so a pack does not claim an absent component is installed. The first curated groups cover Telegram bridge essentials, translation, progress and cards, radio, and locked content-safety controls.

## Computed transparency card

The card is generated from the resolved inventory records; its prose description is never used as evidence. It shows:

- every resolved target and its current on, off, locked, or unknown state;
- the indexed instruction excerpt, labelled as an excerpt whenever it is shorter than its source, together with its source path;
- tools, and hooks only when the hook is actually configured, with the event at which it fires;
- network domains the items may contact;
- files or state they read and write;
- requested permissions;
- applicable agents (Claude, Codex, or AGY when the scanner reports them); and
- estimated prompt context from the readable source files' real character counts (never from their excerpts), and **unknown** when a source cannot be read; the token figure estimates one token per four characters.

When source metadata does not establish a field or state, the card says **unknown**. HarnessGram does not infer domains, permissions, file access, hook timing, agent coverage, or state from a friendly description. This conservative boundary matters because agent packages can hide instructions, steal secrets, or introduce prompt injection. Definitions should remain reviewed and version-pinned with the environment that supplies their items.

## Read-only controls

`harnessgram packs list` and `harnessgram packs show <id>` inspect installed packs. There are no `packs on` or `packs off` commands, no pack change log, and no pack-level undo. `/api/packs` accepts only the view action and uses the Cockpit's normal owner authentication as a read-only route.

In the Cockpit, each knowledge item or add-on in a card links to its existing row. The owner changes it there, using the individual control that already supplies undo and lock protection. Locked items remain visible with a lock and cannot be changed. Hooks and other non-switchable facts are displayed without inventing a control.

The Cockpit places **Packs** beside **What the system knows** and **Add-ons** in Settings. “What this pack contains” opens the computed card.

## Later stages

Pack-level switching is deferred to a later stage, rather than being part of Stage 1. A later sharing design may add curated submissions only after a scan and independent second-agent review; Stage 1 has no store or remote source.

## סיכום בעברית

חבילות מציגות בשלב הראשון, לקריאה בלבד, יכולות שכבר מותקנות במחשב של הבעלים. כרטיס השקיפות מחושב מהפריטים עצמם ומציג „לא ידוע” כשאין מידע. כל פריט מקשר למתג הקיים שלו, שבו כבר קיימות הגנות נעילה וביטול. הפעלה או כיבוי של חבילה שלמה נדחים לשלב מאוחר יותר.
