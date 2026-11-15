# Cockpit v2

Direction D adds a sky header, glass surfaces, four navigation tabs and a new
Now screen (milestone 1), then Projects, a project page, Tools and Settings
(milestone 2). The existing panel remains the default. No screen replaces the
app authentication model.

## Enable or roll back

In panel Settings, use **Preview the new Cockpit** (Hebrew:
**תצוגה מקדימה לקוקפיט החדש**). The closed `ui_v2` choice has three values:

| Value | Result |
| --- | --- |
| `off` | Default: the existing panel. |
| `preview` | New shell and Now screen, only after owner authentication. |
| `on` | New shell and Now screen for the panel's authorized users. |

The panel currently authenticates only its owner. Preview eligibility is emitted
by the server after the existing signed Telegram authentication check. Neither a
URL parameter nor an app token can enable it. All mutations still go through the
existing owner APIs. Switching off restores the old UI without deleting cards,
decisions, request history, drafts or reminders. The switch is stored privately
and atomically; a missing or corrupt value fails closed to `off`.
The same three-option control is available in both classic and v2 Settings,
including when the stored choice is absent, so switching off leaves a path back.
The bottom navigation stays fixed to the viewport and clears both the browser
and Telegram home-indicator insets; page padding keeps the final content clear.

## Source and authority

- Now begins with every open decision, including old decisions. Ask and choice
  cards retain their original answer controls. Ended/replaced decisions remain
  labeled and disabled; the server independently refuses stale approvals.
- Yes/no answers retain the precise original labels, decision ID, note, queue
  handling and delivery receipt. A queued answer is never labeled delivered.
  Recommendations appear only when explicitly supplied in the source text;
  otherwise the UI says no recommendation was supplied.
  Full decision details are always visible before the answer buttons; the More
  disclosure contains only optional source text, topic links, age and notes.
- Requests use the existing Done, Not relevant and Push forward actions. Each
  needs a second tap within four seconds. The project and request ID are passed
  unchanged. The Older disclosure contains request history and old reminders;
  open decisions are never hidden inside it.
- Reminder clearing and restoring use their original opaque references and APIs.
- Project status is computed by the bridge: waiting for the owner takes priority,
  then blocked, then explicitly completed, then on track. Progress comes from a
  progress card, its steps or a checklist. Unknown progress is a dash with an
  accessible explanation, not a fabricated percentage. A completed request is
  insufficient evidence that an entire project shipped. A stale/auto-closed
  progress card cannot establish shipment. Each tile exposes the source age in
  its accessible description, including a warning when the source is old.
- V2 project data is derived only from the existing classic response. Enabling
  V2 never runs the Morning Brief's additional readers when its switch is off;
  checklist and brief status data are used only when already included in classic.
- New questions update the count and content without changing the active tab.
  A direct question link still opens and highlights that exact question. Notes
  use the existing draft and exit confirmation machinery.

## Milestone 2 screens

All controls below are the classic controls, moved and restyled, or call the same
authenticated API with the same payload. The bridge still computes every status.

- **Projects**: one card per project with its status picture and stamp, progress
  ring (or the unknown dash), one sentence of status, a "waiting for you" count and
  the update time. Collapsed **The team** (working / waiting / finished counts, then
  the classic session rows with all their actions), **System status** (weekly quota
  bars first; computer and usage inside) and, when present, idle team items.
- **Project page** (tap a card or a Now tile): header picture and ring, every open
  decision of that project with full detail before yes/no, the newest progress
  card's steps, topic rows that open the existing topic sheet, the agents' https
  links, that project's reminders, its live conversations with "Open in
  Telegram", and finished topics folded. It is one back-aware layer: the header
  arrow, the Projects tab and native/browser back all return to the list at the
  same scroll position. `presentation()` adds only references (`sessions`,
  `topics`, `reminders`) to each project; topics and reminders never create one.
  An **Ideas waiting** card lists the project's open ledger items older than a day;
  a tap shows the item and can ask the project's lead session about it (see
  "Recall after a compaction" in panel.md).
- **Tools**: a remembered **Studio | Radio | More** choice. Radio moves the same
  persistent player node (one audio element) into a large player, adds the
  brainstorm room and the announcement composer as two disclosure tiles, keeps the
  classic station card (start/stop), the voice update choice (same `voice_digest`
  setting) and a YouTube status row that opens Settings. Studio is the existing
  sandboxed app frame; More lists the other installed apps.
- **Settings**: five collapsible groups (messages and recordings, automatic work,
  models and quotas, radio and sharing, connection and upkeep). Rows are built by
  the classic `settingsBox` and placed by their own label; a switch added later
  falls back to Automatic work (guarded by a test). Connection holds the session
  reload card. The preview switch stays visible below the groups, never folded,
  so switching off is always one tap away.

## My schedule

The Now screen shows a "My schedule" card right after the open decisions: Today and Tomorrow tabs, the owner's own
items and read-only calendar events (timed first, then undated), a one-tap done/undo button per own item and an
Add button that opens a one-field sheet. Its data comes from `POST /api/schedule-view` (not the state poll) and its
changes from `POST /api/schedule`; the store, the Hebrew/English day and time reader, the `/todo` command and the optional
calendar source are described in docs/panel.md ("My schedule"). The card uses the shared spacing system and is covered by
`tests/test_cockpit_v2_schedule_node.py` and `tests/test_cockpit_v2_schedule_browser.py`.

Under the schedule rows the same card shows today's home chores and the daily routine as checkable rows, and a
**Family board** button that opens the week, the shopping list and the meal plan in a sheet, with an A4 Hebrew print view.
Its data comes from `POST /api/family-view` and its changes from `POST /api/family`; the store, resets, Shabbat rule and
`/shop` are described in docs/panel.md ("Family board"). Code: `cockpit_v2_family.js` and `cockpit_v2_family.css`; tests:
`tests/test_cockpit_v2_family_node.py` and `tests/test_cockpit_v2_family_browser.py`.

## Work map and Good morning

A project page has a **Work map** card after its open decisions: one line per piece of work (what it is, waiting for
you / in progress / done today, and what it changes for you), waiting ones first, six shown and the rest behind "Show
all". Now shows a dismissable **Good morning** card above the open decisions once a day (the morning briefing). Both
come with the state poll (`cockpit_v2.projects[].work_map`, `state.morning`); the only action is
`POST /api/briefing-dismiss`. Neither opens a sheet. Sources, the pinned message, `/what`, `/briefing` and the add-on
flags are in docs/panel.md ("Work map and morning briefing"). Covered by `tests/test_work_map.py`,
`tests/test_briefing.py`, `tests/test_cockpit_v2_workmap_node.py` and `tests/test_cockpit_v2_workmap_browser.py`.

## Spacing and responsiveness

All v2 screens share one spacing system, defined as custom properties on
`:root.cockpit-v2` in `cockpit_v2.css`: `--v2-gutter` (16 px on phones, 24 px from
600 px), `--v2-pad` and `--v2-pad-sm` (card inner padding, 12-16 px), `--v2-gap` and
`--v2-gap-sm` (grid and list gaps) and `--v2-section` (space above a section
heading). `--v2-edge` is the page edge that the page and every edge-to-edge band (sky
header, page heads, chips, sheets) measure from; the header, tab bar and sheets
add the top/bottom safe-area insets. Logical properties only.

**The work at a glance** (Now) shows project tiles in 2 columns below 430 px,
3 columns from 430 px and as many 150 px+ columns as fit from 700 px. A tile
is its picture (with the status stamp), the name (two lines at most, the same
reserved height in every tile of a row) and the progress ring beside the status
and age lines, never on the picture. Raw session-title slugs are displayed as
words (`app-store-review-watch` reads "App store review watch"); configured
names are shown as given and no key or stored value changes. Projects waiting
for the owner come first, then blocked ones, then the most recently updated.
"Waiting for the owner" counts open decisions and question cards plus owner
waits from the request ledger or the brief's work lines (the larger of the two,
since both can list the same ledger items).
Quiet projects (no update for 24 h and no reported progress, nothing waiting for
the owner) fold into **Less active (N)** below the grid.

## Public illustration catalogue

Fourteen optimized, metadata-free WebP assets (nine scenes plus five Settings
group pictures cropped from the approved design) are packaged with `ctb`. Only the fixed
catalogue at `/assets/cockpit/v1/<name>.webp` is public. It contains no project
names, state, private media or user data. The route is independent of the bridge
switch, rejects paths outside the catalogue and returns `nosniff` plus
`Cache-Control: public, max-age=31536000, immutable`. Bump the URL version whenever
the image bytes change. The normal nonce-based Content Security Policy is intact.

Configure project-to-art mapping in the private bridge config, alongside the
existing brief settings:

```json
{
  "brief": {
    "project_art": {
      "alpha": "plane",
      "beta": "book",
      "gamma": "books",
      "delta": "visor"
    }
  }
}
```

Allowed project scenes: `plane`, `book`, `books`, `visor`, `clapper`, `catalog`,
`generic`. A valid `project_art` entry always wins. Without one, the bridge picks
the scene itself, with no configuration: first from generic subject words in the
project's key or display name (`ART_WORDS` in `cockpit_v2.py`, e.g. flight →
`plane`, lesson → `book`, library → `books`, headset/bridge → `visor`,
studio/video → `clapper`, store/catalog → `catalog`), then by a stable spread
(a SHA-256 of the key picks the start; projects without a word match get
scenes not already used by the others, independent of order). `generic` is
used only when there is no project at all. Owner project names never appear in
the package (the public-hygiene deny-list); a private `project_art` entry is the
way to pin an exact scene. Invalid mappings are ignored and the automatic pick applies. Project matching uses the
bridge's existing project keys; display aliases use existing `project_names`
configuration. Owner-specific mappings belong in private configuration, never
in the package or public tests. Status overlays are computed independently of
the decorative illustration.

The existing persistent radio audio element is reused. It moves after the Now
content rather than before the decision zone, so tab switches and polls do not
interrupt listening. Media Session uses the approved `artwork.webp`, title
`רדיו הצוות` and artist `Harnessgram`. It never starts or publishes a station.

## Validation and fidelity review

The must-keep contract is the JSON list in
`tests/cockpit_v2_must_keep.json`; entries with a `screen` field belong to the
milestone 2 screens and are checked by `tests/test_cockpit_v2_screens_browser.py`
(also no horizontal overflow at 390 px and 44 px touch targets). The browser tests exercise it at 390 px in
Hebrew and English, light and dark. They check all open IDs, answer controls,
request actions, a reversible Older disclosure, reminders, four tabs, a way out,
minimum touch heights and horizontal overflow. Other cases cover old decisions,
notes, preview rollback and new questions arriving while Settings is open.

Backend tests run real request handlers through an in-memory transport, including
HMAC authentication and the public asset allowlist. Node tests execute the new
screen and the existing request controls for semantic and payload checks. These
tests are useful where Chromium is unavailable; they do not prove visual layout.
`tests/test_way_out.py` also inventories the new exit and disclosures.

## Old topics

Now shows an "Old topics (N)" card when the bridge finds forum topics nobody has used for `topic_cleanup_days`
days (default 14, in the settings file). A topic is listed only when it has no session activity or messages
for that long, nothing waits on the owner in it (decision, card, reminder, unticked checklist item), it is
not the General topic, a scheduled task's shared topic or one with a pinned link on its board, and the owner
has not tapped **Keep** (kept forever). A topic whose last activity is unknown is never listed.

Every session on a topic must be ended and quiet (one working, running, moving or with a queued owner message keeps the topic out). Telegram's own pinned messages cannot be read through the Bot API, so "pinned" means a link pinned on the topic's board. Each row has **Keep** (with an undo) and **Close**; **Close all** asks for a second tap that names the count. Closing is
Telegram's `closeForumTopic`: reversible, and writing in the topic opens it again. Nothing is ever deleted.
The result card has **Reopen** as an undo. `POST /api/topic-cleanup` is owner-only with the fresh action login;
every close, keep and reopen is appended to `topic-cleanup-log.jsonl` in the state folder (session prefix,
place and result only).

Run the focused checks:

```sh
python -m pytest -q tests/test_panel*.py tests/test_cockpit*.py \
  tests/test_way_out.py tests/test_topic_cards*.py tests/test_public_hygiene.py
```

Run the full suite with `python -m pytest -q`. Browser tests require a supported
Chromium installation and a host permitted to launch it; HTTP integration tests
also require loopback sockets. A skipped browser test is not a fidelity pass.

For deterministic, private-data-free screenshots and side-by-side/difference
images against the approved designs:

```sh
PYTHONPATH=src:tests python tests/cockpit_v2_screenshots.py --milestone 2
PYTHONPATH=src:tests python tests/cockpit_v2_screenshots.py --milestone 1
```

`tests/test_cockpit_v2_layout_browser.py` walks Now, Projects, a project, Tools,
Settings and the knowledge home at 360, 390, 430, 768 and 1024 px (Hebrew and
English, light and dark) with a crowded fixture and fails on horizontal
overflow, a card closer than 16 px to the edge, text painted outside its card,
a tap target under 44 px or a wrong project-grid column count. Milestone 4 of
the screenshot helper (`--milestone 4 [--width N]`) captures the same screens
with that fixture into `v2-spacing/after/`.

Milestone 2 writes Projects, the project page, Tools (Radio and Studio) and
Settings to `~/.codex/state/harnessgram-redesign/build-m2/`; milestone 1 writes Now
to `build/`. Override with `--out <directory>` and `--hifi <approved-design-dir>`. The helper
saves full-page and viewport PNGs for each language/theme, a JSON receipt, and the
Hebrew/light comparison and difference images. Pixel differences are a visual
aid, not an automatic acceptance threshold: source text, large touch targets
and full open-decision coverage may change the page length. Independent fidelity
review and approval are still required before rollout.
