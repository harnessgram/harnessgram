# The Studio (a Cockpit app)

The Studio is a private-tool style Cockpit app that ships with the repository: a tab in the control panel for
directing videos from the phone. It is built in steps: the app shell and its home screen (step 2), and the desk of one
video (step 3).

```
harnessgram studio install     # writes <state dir>/cockpit-apps/studio/ ; run it again after an update
```

Reopen the panel and the Studio tab (film icon) appears. The install writes three files (`manifest.json`,
`index.html`, `api.py`), starts nothing and changes nothing else. The owner's draft marks live in `state/` inside the
same folder; a new install replaces the three files and keeps them.

## What step 2 has

- **The shell.** An ordinary Cockpit app (`docs/panel.md`, "Cockpit apps"): the page asks the panel for `hello`, calls its
  API through the panel and uses only the panel's own messages. Sub-pages ask the panel to fold its header and tabs
  (`focus`), take over the native back button (`back`, always answered with `back_done`), show the waiting count on the
  tab (`badge`) and open the topic's chat (`open_chat`). The page pads its bottom dock with the safe-area inset the
  panel reports, so the main action stays in thumb reach. The panel keeps its own exit if the app ever fails to answer.
- **The home screen ("the director's desk").** What waits for you (a card stack with a preview, the series, the
  episode, the length and what the team suggests), what is in progress (a stage track), the way to a new video, and the
  shelf with a series filter. Phone first (390 px), light and dark from the Telegram theme, Hebrew (RTL) and English,
  logical CSS only, counts spelled out in words on Hebrew lines, numbers and percentages through `Intl`.
- **Every state.** Loading (skeleton), empty (no videos yet, with the ready templates), offline (what was loaded stays
  under a notice), bridge off (the designed off state, no stale videos shown), expired login, a generic error with a
  plain reason, and no permission.
- **Entry points of later steps.** "New video", "approve as is", a shelf item and "all" lead to one designed page that
  says the step is on its way and that nothing was sent or changed.

## What step 3 has: the desk of one video

"Open and direct" (or the preview) on a waiting video opens its desk, a sub-page with its own back bar; the panel's back
button walks the pages one by one (words view, desk, home) and every back is confirmed.

- **The player and the sentence map.** The still and caption of the sentence in the player; play steps through the
  stills in time (stills and captions, no sound until a real draft brings its proxy video) and skips removed sentences.
  Under it the map: one cell per sentence, as wide as its length, in one status vocabulary (kept, shortened, team
  suggestion, new voice or picture, needs a fix, removed); a tap on a cell moves the player.
- **Suggestions, one at a time.** The team's suggestion as one card: the sentence with the part that goes struck, the
  shorter wording (a tap opens it for editing), the reason, and keep / shorten / remove with the suggestion as the main
  button. Answering moves to the next one; when none is left, the card says so.
- **The storyboard.** The scenes in order, a still each, with their length, sentence count and what is open or changed;
  a scene opens the words view at that scene.
- **The words view.** Every sentence, scene by scene on its own film strip: still, timecode, hear, the team's pills
  and keep / shorten / remove; a removed sentence shows struck with "restore". The legend and the runtime (old length,
  new length, what is saved) sit with the map.
- **The dock.** How many changes and the new length, "to review" and "prepare a new draft". Both open the "coming next"
  page of their step (review: step 6; render: step 4), which says the marks are kept as a draft and nothing was sent.
- **States.** Loading (the poster first, then the rest), offline with the loaded video still shown and every mark button
  disabled (nothing pretends to save), offline with nothing loaded, not found, a generic error, expired login, bridge
  off and no permission.

A mark shows at once, is stored, and rolls back with a plain notice when the Studio did not keep it. The shorter-wording
field takes the page's direction while empty and turns `auto` only with text.

## The API

| op | in | out |
|---|---|---|
| `desk.get` | | `desk` (read-only) |
| `project.get` | `project` | `project`, `marks` (read-only) |
| `marks.set` | `project, i, mark: keep\|tighten\|remove\|clear, text?` | `marks` |

`marks.set` is the only write: a local draft mark in `<app folder>/state/marks.json` (folder `0700`, file `0600`). It is
validated against the provider's own video (a known id, a sentence it has, one of the marks, a plain text of at most
240 characters only with `tighten`), written under a lock, and never through a link (`safefs.py`, the same writer the
install uses). Nothing renders, is sent or is published; there is no new route. While the bridge is off the panel
answers `503` before the command runs, and the app shows its off state.

## Where the data comes from

`ctb/studio/provider.py` has two functions, `desk()` and `project(id)`. With no adapter configured they return a small
invented sample (the page shows a notice whenever `sample` is true). An optional private adapter supplies real data
using the shapes documented at the top of that file, so the page does not change. Each scene and sentence
already has a `frame` (the engine's real still, `--frame`) and each sentence an `audio` (its own clip): paths under the
app's media root, `None` until they exist. When they are there the page draws the still over the placeholder and plays
the clip through `media/<path>?auth=`; a path that is not a plain relative path is ignored.

## Step 4: a private data adapter

Set these optional fields in the bridge's existing private configuration, then run `harnessgram studio install`:

```json
{
  "studio_adapter": "/srv/private-studio/adapter.py",
  "studio_media_root": "/srv/private-studio/media"
}
```

Both default to `""`. `studio_adapter` is an absolute path to trusted Python code outside the public repository.
It exposes `desk()` and `project(id)`; `project` returns `None` for an unknown id. `studio_media_root` overrides the
adapter's optional absolute `MEDIA_ROOT` directory. No media root means frames and audio stay `None`. The root must
already exist and meet the Cockpit host's protected-folder policy. The install pins the root in its manifest and API
wrapper, along with the selected configuration path, so both agree even with a custom configuration directory.
Reinstall after moving or changing the root, or upgrading the installed API wrapper. Clearing `studio_adapter`
restores the sample; draft marks are kept.

An adapter runs with the app user's normal permissions: this is a trusted local extension, not a Python sandbox.
Only its documented data fields reach the page. The loader drops malformed rows, duplicates and unknown series;
checks finite times, ranges, identifiers and text; and bounds lists (50 home items per category, 60 scenes and 200
caption cues per project) and the JSON response size. `scene_i` is zero-based; scene and sentence `i` start at one.
Surviving sentences keep their original row numbers when malformed rows are dropped. Gaps are valid: saved marks,
selection and playback use those IDs rather than positions in the filtered list.
Production projects without subtitle cues keep an empty sentence list; the current desk cannot play those projects
yet. No transcript or suggestion is invented to fill the gap.

Frames and sentence audio must be existing regular files, using plain relative ASCII paths below the media root
(at most 200 characters). Absolute paths, dot segments, hidden names, backslashes, missing files and links below the
root are rejected. Reads reuse `safefs` with directory descriptors and `O_NOFOLLOW`, and create nothing. Media uses
the Cockpit's existing authenticated route, which streams the verified descriptor directly without reopening its
path. Adapters remain responsible for reading their own source files safely.

The dedicated API process applies a 256 MiB memory limit before importing the private adapter (address space where
supported, or the data limit on macOS), preserving stricter inherited limits. Import and call diagnostics go to the
null device rather than a memory buffer. Memory exhaustion, or a host that refuses both resource limits, returns the
labelled sample with reason `call`; the private adapter is not loaded when no limit can be applied. This limits the
API subprocess only, not the panel process or an installer importing trusted adapter code to discover its media root.

Import, call, configuration and top-level shape errors return a labelled sample with a bilingual `notice`. Because
the existing page has no notice field slot, the same safe reason appears in its existing first card/project title.
Exception text and local paths are never returned. Draft mark writes are refused while a project is an error
fallback, so sample sentences cannot become marks on a real video. The ordinary unconfigured sample remains editable.
Loading reads configuration without migrations or credential commands, and reloads adapter code when its file changes.

Keep owner folders, engine conventions and the adapter itself outside the public repository. `.private/` is ignored
for local handoffs; it is never part of a distributable adapter implementation. This step starts no engine process,
renders nothing and adds no publication action.

## Files

| File | What |
|---|---|
| `src/ctb/studio/strings.py` | The words, one table per screen (Hebrew and English). |
| `src/ctb/studio/kit.css`, `sprite.svg` | The brand kit: the Cockpit's tokens plus the Studio layer, icons and scene placeholders. |
| `src/ctb/studio/app.js`, `page.py` | The page. |
| `src/ctb/studio/provider.py`, `api.py` | The data and the command. |
| `src/ctb/studio/adapter.py` | Generic trusted-module loader, shape validation and read-only media checks. |
| `src/ctb/studio/marks.py`, `safefs.py` | The owner's draft marks, and the writer that never follows a link. |
| `src/ctb/studio/install.py` | Writes the app folder. |
| `tests/studio_must_keep.json` | The must-keep contract: the elements each screen may never lose (`tests/test_studio_app_browser.py`). |

## Placeholders waiting for real art

The scene pictures (`s-*` in the sprite) stand in for poster frames and template stills that the video engine renders
from real drafts; the clapperboard drawing in the empty and "coming next" states is a named slot (`ART_CLAPPER` in
`page.py`) for the final illustration.
