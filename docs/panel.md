# The Cockpit: the control panel (Telegram Mini App)

> **Status: experimental setup.** The panel itself is part of the bridge and has tests, but this repository
> does not yet contain a complete, reproducible way to set it up: `harnessgram install` does not install its
> service (it only restarts a macOS job that already runs it), `harnessgram doctor` does not check that it is
> reachable (it only warns when a running panel is older than the installed code), and the button in the topics
> is detected automatically only from a macOS LaunchAgent. The sections "What it needs" and "Exposure options" below say exactly what you must
> provide yourself. Everything else in the bridge works without it.

## What it is

The Cockpit is a small web page, served by the bridge on your own computer, that opens inside Telegram as a Mini
App. It shows what waits for your answer, what each agent is doing, and the Claude quota, and it lets you answer
decisions and cards, write to a session, and change a few bridge settings from your phone. It is optional and off
until you set it up.

## What it needs

1. **The server**, kept running by you:

       harnessgram panel serve [--port 8765]

   It listens on `127.0.0.1` only (port 8765 unless you change it) and uses the Python standard library. It
   runs in the foreground; keep it alive with whatever you use for services (a LaunchAgent on macOS, a systemd
   user unit on Linux, or a terminal multiplexer). The repository ships no unit or plist for it.
2. **An HTTPS address that reaches that port from the device where you open Telegram.** Telegram loads a Mini
   App from an HTTPS URL, and the server itself speaks plain HTTP on loopback, so something in front of it must
   terminate HTTPS and forward to `127.0.0.1:8765`. See the options below.
3. **The bot's Main App in BotFather**, pointing at that HTTPS address. The button the bridge pins in each topic
   is a link to `https://t.me/<bot username>?startapp=...`, which opens the bot's Main App on that topic. The
   exact BotFather menu names are Telegram's and may change.
4. **The button.** The `panel_button` setting in `config.json` is `true` (always pin the button), `false` (never)
   or `null`/absent (automatic). Automatic means "on only when a macOS LaunchAgent named
   `com.claude-telegram-bridge.panel` exists", so on Linux, or under any other job name, set `panel_button` to
   `true` yourself once the panel works. Set it only after the check below succeeds: a button to an address that
   does not answer leads nowhere.
5. **A forum group for decisions and cards.** `harnessgram decide ask` and `harnessgram card ...` refuse a
   session whose topic is in your private chat with the bot ("decisions need a topic in a forum group"), so the
   sessions that use them must live in a forum supergroup with topics: the home group (`harnessgram pair --group`)
   or a project group. The rest of the panel does not depend on this.
6. **Checking it.** Neither request below carries a secret. On the computer:

       curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/
       curl -s -o /dev/null -w '%{http_code}\n' -X POST -d '{}' http://127.0.0.1:8765/api/state

   The first prints `200` (the static page), the second `403` (no valid Telegram login, no data). Repeat both
   against your HTTPS address with the same results, then open the bot's Main App from Telegram on your phone.

## Exposure options

The panel's only protection against the outside is its own login check (see "Security model" below): every
action needs Telegram's signed `initData` for the paired owner, fresh within 15 minutes (1 hour for reading), or
the server answers `403` and shows nothing. That check is the same wherever you put the server; the options
differ in who can even send it a request.

| Option | Who can reach it | Trade-offs |
|---|---|---|
| **Private network, for example Tailscale Serve** forwarding an HTTPS port to `127.0.0.1:8765` | Only devices on your own private network (your phone must have the network client running and connected). | Smallest exposure. Needs that client on every device you open the panel from. The repository does not script it: follow the tool's current documentation for the exact command. |
| **Public HTTPS: a reverse proxy, a tunnel, or a public funnel feature** forwarding to `127.0.0.1:8765` | Anyone on the internet can send requests to the page and the API; only the owner's signed login gets data. | Works without any client on the phone. Adds attack surface: this small server is then internet-facing (it limits bodies, connections and time, refuses chunked bodies and logs nothing, but it is a plain standard-library server, not a hardened web server). A forged login needs your bot token, so a leaked token breaks this protection. Do not let the proxy log request headers: `X-Telegram-Init-Data` is a signed login that can be replayed until it expires. The same login also travels in the URL of a Cockpit app's page and media (`?auth=...`): drop or strip the `auth` query parameter from the access logs of any proxy or tunnel in front of the panel (see "Security model"). The proxy must send a `Content-Length` and no chunked request bodies. |

Nothing in this repository turns on a public route. If you use any option, forward only this one port, and never add
`web_app` buttons that open other sites (see "Security model").

## What it shows

The panel opens on what is waiting for the owner, then the sessions and the Claude quota per account:

The Mac resource card follows the owner's decisions and waiting items. Its process CPU figures are lifetime
averages, not current CPU usage. The disk figure is for the system volume only and excludes purgeable space.
Process commands are used locally for classification and are never included in the state. Resource collection is
asynchronous and cached per process, so the first full state can show unavailable until a refresh completes;
lightweight state callers omit this card and do not start collection.

1. **Since you last looked**: a short digest (new decisions, sessions that finished working, new and ended
   sessions), compared with what this phone saw on its previous visit (kept in the page's local storage).
2. **Waiting for you**: one card per open decision (see below) with big yes and no buttons and an optional
   note behind "add a note". A tap shows a confirmation card and a toast.
3. **Reminders**: the open reminders of every session's pin board (`ctb board remind`). Each has a done button
   that clears it on the board, as the board's own button does and without waking the session (`POST
   /api/remind`), and old ones can be cleared with one tap.
4. **Active sessions**: state dot, title, model, context size (highlighted near the auto-compact limit),
   background agents and commands still running, account and last activity. Ended sessions are folded.
5. The team radio switch (when the radio is configured), the quota bars with their reset times, and
   **Model for new sessions**: the `model` key of Claude Code's user `settings.json` (`default_model.py`;
   only that key changes, the file is replaced atomically, a damaged file is never overwritten).

**Hebrew.** Agents write decisions, board items and session titles in English. With `ui_language` `he`, the
state carries a Hebrew copy beside each such field (`title_he`, `detail_he`, `yes_he`, `no_he`, `topic_he`,
`text_he`, `name_he`), read from the translation cache (`translate.cached`) without waiting. A text that is not
cached yet comes back as `null` and is translated by two background threads with the normal chain (never the
local MLX model, network engines only when external translation is allowed); `pending` counts them and the
page asks again after 2.5 seconds. The page shows the Hebrew and keeps the original under a small "מקור"
toggle. All UI copy is in one strings table (`panel_page.STRINGS`), the CSS uses logical properties only and
the colours follow Telegram's theme parameters (light and dark). Pull down or tap the refresh button to
refresh; the page also refreshes by itself: every 10 seconds while it is visible and a session works, every 20
seconds while it is visible and nothing works, and from once a minute up to once every five minutes while it is
hidden. Transcript reads behind `/api/state` are cached by each file's size and mtime, so a frequent refresh reads
only what changed.

**Automatic progress cards.** A working session whose agent made no progress card of its own gets one from the
bridge (`auto_progress.py`, marked `automatic` in the card data): the topic's name as its title, the subjects of
the session's task list (TaskCreate / TaskUpdate or TodoWrite in the transcript) as its steps and percent, and
how many background tasks it runs. No tool output or file contents reach it. It is replaced as soon as the agent
publishes its own card and closed when the session goes idle (with no background task left) or ends. The hooks
also remind a session on a long task (background tasks, three minutes, or 25 tool calls in a turn) that has no
card of its own to publish one, at most once every ten minutes.

**Newest first, stale folded away.** Decisions and reminders come newest first. A decision whose session has
ended, or that the same session asked again later (the same words once ids and numbers are dropped), is marked
`stale` (`ended`, `replaced`; `panel_view.stale_reasons`) and shown in a folded "No longer relevant" list with one
"clear all" tap (`POST /api/dismiss {"ids": [...]}`; the server closes only ids it counts as stale itself, as
cancelled, without telling the session). New decisions carry a "new" badge until "mark all as seen" (kept in the
page's local storage).

**Older items, one collapsed group.** Everything that has waited for the owner longer than three days (decisions,
choice and question cards, reminders) moves into one collapsed group titled "Older" (Hebrew "ישנים") with its count,
after the waiting items and before the working sessions. The group is closed until tapped, remembers its state on
that phone (local storage, a convenience only) and is not drawn when nothing is old. Inside, the items work as
before: an old decision or card can still be answered, and an old item never carries the "new" badge. The tab
badge, the summary ring and the "waiting" count include only the fresh items. The server decides what is old, from
the item's own timestamp and its own clock: `panel_view.OLD_AFTER` (three days) is the one threshold, an item is old
only when it is strictly older than that, and an item without a usable time counts as fresh. `/api/state` marks each
decision, card and reminder with a boolean `old`. A decision that is only old (its session still alive, not asked
again) also counts as stale for `/api/dismiss`, so the group offers one two-tap "clear the old ones" button for old
decisions and old reminders.

**The whole team at a glance.** Each session row shows, for a working session, the latest step of its live
activity card (`progress/<session>.jsonl`) and, for an idle one, the "Summary:" line of its last reply (or its
first line), translated like the rest. Rows open in place for model, context, background tasks, account,
last reply and a button that opens the topic (`url`, a `t.me/c/...` link, also on decisions and reminders).
"Working now" comes first, then "finished recently" (idle under 3 hours), with older idle and ended sessions
folded.

**Bridge settings** (`panel_settings.py`, `POST /api/setting {"name", "value"}`): the same switches as the bot's
global settings menu, through the same helpers: away mode, voice-note confirmation, delivery at the end of a
reply, a new status card per reply, a notice per background agent, the separate agents topic, asking before
routing General, and the auto-compact limit (one of its fixed steps). **Radio words**: with the radio on, the
owner can type a line for it to say now (`POST /api/radio-say {"text"}`, `ctb radio say`).

Stopping or restarting a session and starting a compaction are not in the panel: in the bot they run inside
the listener's button handlers, which drive the session's terminal directly, and there is no queue the panel
could hand them to without running tmux itself. Only a session's own automatic-compaction threshold can be set
here.

## Routes

The server answers `GET /` (the page), the Cockpit app routes below and these `POST` routes, nothing else. The
page is served without a login: it is a static document that holds no data, and it shows data only after it
sends the signed login to `/api/state`. Every other route needs the signed login described under "Security model";
the body limit is in bytes. The list is the route table in `panel.py`, and
`tests/test_panel_docs.py` fails when a route there is missing from this file.

| Route | Body at most | What it does |
|---|---|---|
| `/api/state` | 64 | Returns everything the page shows: decisions, cards, reminders, sessions, quota. |
| `/api/decide` | 1280 | Answers one decision yes or no, with an optional note of up to 300 characters. Refused with `409` when the decision is closed, its session has ended or the same session asked it again later; age alone does not refuse it. |
| `/api/card` | 1280 | Answers one choice or question card. |
| `/api/say` | 2600 | Writes up to 600 characters to one live session. |
| `/api/unsay` | 64 | Takes back words written to a session that it has not read yet. |
| `/api/dismiss` | 1280 | Clears decisions the server itself counts as stale (closed as cancelled, the session is not told). |
| `/api/remind` | 2600 | Clears up to 30 reminders on their boards, without waking sessions. |
| `/api/close` | 1280 | Closes the Telegram topics (up to 20) of sessions the server counts as left behind (open, untouched for a day). The terminal is not touched; writing in the topic reopens it. |
| `/api/screen` | 64 | Returns the bridge's own redacted copy of one live session's terminal screen; nothing is typed into it. |
| `/api/compact` | 64 | Sets one session's automatic-compaction threshold (the global one, never, or a fixed step). |
| `/api/brief-seen` | 64 | Moves the owner's "since you last looked" marker of the Morning Brief forward (never back, never into the future). |
| `/api/block-mute` | 64 | Mutes or unmutes one session's agent blocks in the Morning Brief. |
| `/api/model` | 64 | Sets the model new sessions start with. |
| `/api/setting` | 64 | Changes one bridge setting. |
| `/api/radio` | 64 | Turns the team radio on or off. |
| `/api/radio-say` | 2600 | Gives the radio a line to say now. |
| `/api/room` | 2600 | The radio's brainstorming room (off by default; see docs/radio.md): `start` {`topic`, `minutes`, `project`?}, `pause`, `resume` {`text`?}, `summarize`, `end`, `close`, and `send` {`room`, `task`, `session`}, which claims one task of that room once (a second tap, a stale page or a retry queues nothing) and queues it to one live session as a held proposal that approves nothing. |
| `/api/bank` | 64 | Sets the saved-reset count of a provider. |
| `/api/youtube` | 512 | The YouTube staff radio card's actions (see "YouTube staff radio" below); while mirroring is off only `stop` and `disconnect` pass. |

The Cockpit apps (see "Cockpit apps") add three routes, all answered before any lookup of the app, so a request
without a valid login learns nothing about which apps exist:

| Route | Login | What it does |
|---|---|---|
| `GET /app/<name>/` | the signed `initData` in the header or in the `?auth=` query, valid for reading (1 hour) | Serves the app's one page, with its own strict Content-Security-Policy. |
| `GET /app/<name>/media/<path>` | the same as the page (header or `?auth=`; an iframe, `<video>` or `<img>` cannot set a header) | Serves one media file from the app's `media_root`, with HTTP Range. |
| `POST /app/<name>/api` | the header only, valid for acting (15 minutes) | Runs the app's `command` with the JSON body on standard input. |

The page is Hebrew and right to left unless `ui_language` is `en`, and it follows the Telegram theme colors.

## Decisions

An agent asks from inside its session:

    ctb decide ask "Ship the fix?" [--detail TEXT] [--yes LABEL] [--no LABEL]
    ctb decide list
    ctb decide cancel <id>

**Which session.** The command acts only for the session it runs inside, and proves it: the session id in
`CLAUDE_CODE_SESSION_ID` must have a live Claude Code process in Claude's own session records (any
account), and that process must be an ancestor of the calling process. A tmux session name or pane proves
nothing. There is no `--session` flag. A session that cannot be verified (no recorded process) or that is
not running inside the named session's process is refused with a clear error, so one session cannot post,
list or cancel as another. The SessionStart hook (`hook-start`) applies the same rule before it saves a
tmux pane for a session: it changes nothing unless it runs inside that session's Claude process (when one
is recorded), and it saves only the caller's own pane (the pane's first process must be one of its
ancestors).

A decision is `{id, session, chat, thread, title, detail, yes, no, created, state}` in
`decisions.json` in the state dir (private file, file lock, because the CLI and the panel are separate
processes). The title is at most 200 characters, the detail 1500 and each label 30 (default Yes/No, or
כן/לא); anything longer is refused, not cut. At most 50 decisions are open at once, at most 5 per session.
A yes label that reads as a no (no, לא, cancel, ...) or a no label that reads as a yes (yes, כן, approve,
...) is refused, because the owner only sees the label; matching ignores case, spaces and punctuation.
`ask` also posts one short line in the session's topic ("a decision is waiting in the control panel").
`list` and `cancel` see only the session's own decisions. Answered and cancelled decisions are kept
(newest 50) so a late tap gets a clear refusal.

The owner taps yes or no in the panel (`POST /api/decide`, body `{"id", "choice": "yes"|"no", "note"}`,
note at most 300 characters). The answer is recorded once; a second answer, a cancelled decision or an
unknown id is refused (`409`, which names the recorded choice, or `404`). The answer is saved as closed
first and then queued to the session's inbox; if the message was not written the decision is reopened
under the same lock (an accepted answer is never reopened; a failed trim of the queue is ignored). The
progress boundary is added only after an accepted answer. The panel waits at most about 2 seconds for the decisions and inbox locks and then answers `503`.

**What the session receives.** An inbox message with `source: "panel"` (not `owner`), framed under its own
header as a tap and not typed words, in fixed server wording: "The owner tapped YES (button: «Ship it») in
the control panel." followed by the question it asked and the owner's note, each quoted in « » as text
that someone else wrote. A notice with the choice (not the note) is posted in the session's own topic in
its own chat, so a project-group session is answered in its group; the note goes to the session only.

**Approval.** An answer in the panel counts as the owner's own word, because the panel server verified the
request first (signed by Telegram, a few minutes old, for the paired owner). It approves exactly what its own
question names and nothing else: a yes to "Ship the fix?" approves shipping that fix, and does not approve
anything the session asks about afterwards. The text the session receives ends with that rule (the same
wording as `framing.PANEL_APPROVES`), and the page tells the owner so under the cards. So a session must put
the one sensitive action, precisely, in the question of a decision about money, production promotion, deletion,
accounts, publication or store submission.

This is guidance given to the model in the text around each message, not an enforced sandbox: the bridge cannot
stop a model that ignores it. What stops an action is the agent's own permission system (Claude Code's
permission settings), which should stay strict. Taps on buttons inside the Telegram chat, voice notes, pictures,
files, forwarded content and messages from team members never approve a sensitive action; only a typed yes from
the paired owner in the session's topic and an answer in this panel do.

## Agent cards

A session can put a live card on the Cockpit from inside its own run (the session id comes from Claude
Code's environment, so a card always belongs to the session that made it):

```bash
harnessgram card progress "Mobile build" --step Prepare --step Test --step Upload --at 1 --percent 40
harnessgram card progress "Mobile build" --id <id> --at 2 --percent 70     # update it in place
harnessgram card choice "Which design?" --option "Option A" --option "Option B"
harnessgram card ask "Anything to add before I continue?"
harnessgram card link "Preview" https://example.com/preview
harnessgram card numbers "Today" --figure "Tests=124" --figure "Time=3m"
harnessgram card close <id>
```

Cards need the session's topic to be in a forum group, like decisions (see "What it needs").

Show-only cards (progress, link, numbers) can be replaced in place by their own session. A choice or a
question is answered once (`POST /api/card`); the answer is queued to the asking session only, in the server's
wording with the card's title and the owner's words quoted, marked as coming from the panel. Links must be
`https://` with no login part. At most 8 open cards per session and 60 in all.

## Morning Brief (preview)

A second home for the Cockpit, behind the `brief_home` setting (Settings, off by default; off leaves the classic
home exactly as it was). It answers the owner's usual questions before he asks them, in one screen:

- a headline sentence computed from facts (lines moving, lines stuck, items waiting for him);
- "since you last looked": what was finished, what got stuck and which questions are new since his last visit
  (a per-viewer marker the page moves only when he leaves it, so the strip stays put while he reads);
- "waiting for you": decisions, question cards and ledger items that wait on him, each with exactly one button
  (a question opens its own answer card in place; a ledger item opens its line's topic or its details);
- one honest sentence per work line, its source and its age; the colour comes from facts (a progress card that
  stopped moving, no movement for a day while nobody works on it, a live session), never from the sentence;
- "noticed by itself": a card whose session ended or that stopped moving, a job the watcher reported dead, a
  promise that did not move for days, a source it could not read;
- one row for teams (who works, quota about to go unused) and the Mac (only when the Mac reader is installed);
- an ask box with fixed questions that jump to the part of the brief that answers them (free questions and the
  spoken brief come later; their places are kept).

Every fact carries its age and greys when it is past its freshness window, so nothing old looks alive.

Configuration, all optional, under `"brief"` in the config file:

| Key | What it is |
|---|---|
| `ledger_dir` | A folder of `<project>.json` task-ledger files (`{"items": [{"id", "text", "status", "wait", "updated", "notes"}]}`), read only. Without it the staff radio's `ledger_dir` is used. |
| `ledger_command` | Instead of a folder: a command (a list of strings) that prints `{"<project>": {"items": [...]}}`; its answer is reused for 30 seconds. |
| `project_names` | `{"<project key>": "<shown name>"}`; the radio's `project_names` are used too. |
| `events_file` | A JSON-lines file a watcher appends to: `{"kind": "job_died" \| "review_findings" \| "check_failed" \| "note", "line", "text", "at"}`. The last day is shown. |
| `lines` | `"sample"` shows invented sample lines (for a first look on an install without a ledger). |

Without a ledger the lines come from the sessions alone (a session titled `Studio: filters` is on the line
`studio`; a project group or a configured title prefix wins).

### Agent blocks

An agent adds small pieces to the brief with `ctb block post --json <file|->` (plain JSON on stdin, so any
harness that runs inside a proven session can post), updates one by sending it again with its `id`, and removes
it with `ctb block close <id>`. Three kinds in this release:

```json
{"kind": "status", "line": "studio", "text": "Three of eight screens are built. Next: the filters board."}
{"kind": "checklist", "line": "studio", "title": "Screens", "items": [{"text": "Home", "done": true}, {"text": "Filters", "done": false}]}
{"kind": "needs_you", "line": "studio", "text": "The caption style blocks the next step", "card": "<id of this session's open choice/ask card or decision>"}
```

A block is data, never markup: unknown fields, markup and links are refused; every text is cleaned and
credential-filtered when written and again when the page reads it; at most 12 blocks per session and 60 in all;
one update per block every 5 seconds and 60 writes per session an hour; `expires_in` (60 seconds to 7 days,
default 48 hours) removes it. A status block greys after 6 hours, the others after a day, and all of them when
their session ends. The owner can mute a session from a line's details: its blocks fold and it cannot add
anything to "waiting for you". A block never asks or approves anything; `needs_you` only points at a question
the same session asked, and the owner answers there.

## Writing to an agent

Each live agent's details hold a short field. `POST /api/say` `{"session": <first 8 characters>, "text"}`
queues the owner's words (up to 600 characters) to that one session, quoted and marked as written in the
panel. Ended or unknown sessions get nothing, and two sessions that share the short id are never guessed.

Success means queued. Recent answers expose queued, in-flight and transcript-proven delivered states,
including after a reload. A full queue returns a distinct conflict and leaves the answer editable.
See [Cockpit delivery](cockpit-delivery.md) for the full path, recovery rules and regression evidence.

## Reset bank

Some plans come with saved quota resets. No provider reports that number to a program, so the owner keeps
it: plus and minus in the Agents tab (`POST /api/bank` `{"name", "n"}`) or `harnessgram bank set claude 1`.
Sessions read the counts in their hint and are told that spending a reset is the owner's decision, to be
recommended with `harnessgram decide ask`.

**Approval.** An answer on a card and a message written in the Cockpit are framed like an answer to a decision
(see "What the session receives"): they approve only what their own question asks, or, for a message, the one
sensitive action the session last asked about by name. A number in the bank is not an answer and approves
nothing.

## Cockpit apps

A private tool can add its own tab without any change to this code. Put a folder
`<state dir>/cockpit-apps/<name>/` (name: lowercase letters, digits and dashes, up to 24) with a
`manifest.json`:

```json
{"title": "Sample", "title_he": "דוגמה", "icon": "box", "page": "index.html",
 "media_root": "/absolute/media/folder", "command": ["/absolute/executable", "--flag"]}
```

`title` (1-24 characters) and `icon` (one of the page's icon names) label the tab; `page` is one `.html`
file inside the folder; `media_root` and `command` are optional. Any other key, or a rule broken, and the
app is skipped with one line in the log. The tab shows the page in a sandboxed frame
(`allow-scripts allow-forms`, no same origin). The frame asks the panel with `postMessage`:
`{"cockpit": "hello"}` returns the owner's `initData`, language and direction, and
`{"cockpit": "api", "id", "body"}` posts `body` to `POST /app/<name>/api` and returns
`{"cockpit": "api", "id", "status", "data"}`. The server runs `command` (never through a shell) from the app
folder with the JSON body on stdin, a small environment, a 15-second timeout and at most 16 KB in and 1 MB
of JSON out. Media come from `GET /app/<name>/media/<path>?auth=<initData>`: files under `media_root` only,
no dotfiles, no symlink out of it, images, video, audio, JSON and text only, with HTTP Range.
Long jobs are the app's own business: its command starts them detached and answers at once.

A complete example that ships with the repository is the Studio (`docs/studio.md`).

**Page chrome and phone layout.** An app can also ask the page around it for a few things, with `postMessage`
messages of a fixed shape: exactly its fields (a message with any other field, or of an unknown kind, is rejected
whole), from one of the app's own frames. Nothing an app sends is ever drawn as markup, opened as a URL or used as
a style. Everything except `badge` is obeyed only while the app's tab is showing; a hidden app (another tab is
showing, its frame kept alive) cannot change the page around it.

| Message from the app | Effect |
|---|---|
| `{"cockpit": "focus", "on": true}` | Folds the page's header, tabs and refresh strip so the frame fills the screen edge to edge (the top safe-area inset stays); `on: false` brings them back. |
| `{"cockpit": "back", "on": true}` | The native Telegram back button then sends `{"cockpit": "back"}` to the app instead of leaving it (`on: false`, the default: back goes to the home tab). The app must answer each forwarded back with `{"cockpit": "back_done"}` within about a second. |
| `{"cockpit": "back_done"}` | Confirms a forwarded back (the app handled it, for example by going one screen back). |
| `{"cockpit": "badge", "n": 0..999}` | Shows a count on the app's tab (0 hides it). This is the one message taken from a hidden app too: a count on its tab while you are elsewhere is its purpose. |
| `{"cockpit": "open_chat"}` | Opens this topic's chat in Telegram, when the panel was opened from a topic (nothing otherwise). |

**The page always keeps an exit.** If the app does not confirm a forwarded back in time, or on a second back press
before it did, the page leaves the app itself (to the home tab) and drops the app's folding and back takeover. A
page that is loaded or reloaded (it sends `hello`, or the frame's load event fires) starts unfolded: no focus, no
back takeover, no count; an app sends `hello` first and its other messages after the answer. An expired login
(`403` from the state or from an app's API call) leaves the app, hides its frame and unfolds the page before the
"open again" message is shown. Switching tabs, or the app disappearing, restores the header and tabs at once; coming
back to an app that was folded folds it again.

The `hello` answer also carries `focus` and `inset: {"bottom": <pixels>}`, and the page sends
`{"cockpit": "layout", "focus", "inset"}` to the visible app when they change. In focus mode the frame reaches the
bottom of the screen, so the app pads its main action with `inset.bottom` (the home-indicator strip) to keep it in
thumb reach; outside focus mode `bottom` is 0 because the page already keeps the frame clear of it. The frame is
allowed `fullscreen` and `autoplay` (for a video player), and it has no card background of its own, so the app's own
page background reads as the whole page. The page's `film` icon is available to an app's tab like the others.

## Keeping it running and current

The repository does not install a service for the panel; you keep `harnessgram panel serve` running yourself (see
"What it needs"). Two things help once it runs. If it runs as a macOS launchd job named
`com.claude-telegram-bridge.panel`, `harnessgram install` restarts that job on the new code when it is loaded and
running (a stopped or absent job is never started). On any platform the panel records the code it started with, and
`harnessgram doctor` warns when it still runs an older version (fix: run `harnessgram install`, or restart it).
Who can reach it is the choice you made under "Exposure options"; the repository sets up neither a private network
nor a public route. The button the bridge pins in each topic is a link to the bot's Main App
(`https://t.me/<bot>?startapp=...`), which you configure in BotFather.

## Security model

- Every route except the static `GET /` page (see "Routes") needs the raw Telegram `initData` in the `X-Telegram-Init-Data` header; the app page and app media routes also accept it as the `?auth=` query parameter (below). It is
  validated as the Telegram docs describe: `secret = HMAC_SHA256("WebAppData", bot_token)`,
  `hash = HMAC_SHA256(secret, data_check_string)` over the sorted `key=value` pairs without `hash`,
  compared in constant time.
- `auth_date` older than one hour (reading the state) or 15 minutes (answering a decision, and every other
  action) is rejected, and so is one more than 30 seconds in the future (that much clock difference is
  tolerated); the page then asks to close and reopen the panel. And `user.id` must be the paired owner
  (`owner_chat_id`). Everything else gets `403` with no data.
- The bot token and `initData` are never logged or echoed; the server logs nothing.
- Only `GET /`, the three `/app/<name>/...` routes and the `POST` routes in "Routes" exist. GET takes no body, each route has the body limit shown
  there (a 300-character note takes up to 1200 bytes), chunked bodies are refused, a decision's answer body may
  hold only `id`, `choice` and `note`, sockets time out after 10 s, at most 16 connections are served at once, and every
  connection closes after one reply, so a reusing proxy cannot mix requests.
- Telegram gives the same signed `initData` to every web-app address of this bot, so it is valid for an
  hour on any of them: never add `web_app` buttons that open an outside site. A panel left open
  longer than that needs to be reopened.
- A valid `initData` can be replayed by whoever captures it until it expires (15 minutes for actions, 1 hour for
  reading). Keep it out of proxy and access logs.
- The page sets a Content-Security-Policy (the Telegram web-app script and a nonce only) and
  `frame-ancestors` for Telegram, no CORS headers, and `no-store` caching.
- The page renders all data with `textContent`, never as HTML. Full session ids and chat ids are not sent, but
  these are: the first 8 characters of each session id and the session's details (name, state, model, folder, last
  step and summary), a card's and a decision's own random id, and a link to each topic
  (`https://t.me/c/<group id>/<topic>`) that contains the group's numeric id (the page needs the links to open
  topics, and only the paired owner's signed login gets the state).
- Text typed into the panel (a message to a session, a card answer, a decision note) is limited in length and
  characters, but unlike text sent to the bot in Telegram it is not scanned for credentials. Do not type secrets
  there.
- `/app/<name>/` routes need the same `initData`. The page and media take it in the header or in the
  `?auth=` query (an iframe, `<video>` or `<img>` cannot set a header), with the one-hour reading window;
  the app API takes it in the header only, with the 15-minute window. Without it every app route answers
  `403` before any lookup, so it tells nothing about which apps or files exist. An app page gets its own
  policy: inline code only, no network calls, framed only by the panel, sandboxed even when opened alone.
- **The `?auth=` query is a signed login in a URL.** The panel's own page builds such URLs for an app's frame and
  media, and anyone who can read a URL (a proxy's or tunnel's access log, a browser history, a referrer) can use
  the login in it until it expires: one hour after Telegram signed it (`MAX_AGE` in `panel.py`; the 15-minute
  window applies only to header-borne actions). Drop or strip the `auth` query parameter from the access logs of
  any proxy or tunnel in front of the panel, and keep the URLs out of logs you share.

## YouTube staff radio (panel_youtube.py)

`POST /api/youtube {"action": ...}` (owner, fresh login, body at most 512 bytes): `connect` returns a one-time
sign-in link, `enable {"on"}` and `projects {"keys"}` save the card's choices, `arm` then `start {"armed"}` go
live in two taps (the token from `arm` is single-use and lasts 60 seconds), `stop` and `disconnect` run the CLI
detached. While mirroring is off every action gets 503 before the login is read, except `stop` and
`disconnect`, which only reduce exposure.

The sign-in: `connect` issues a ticket (random, single-use, 5 minutes, bound to the owner). The page opens
`<panel_base_url>/oauth/youtube/start?t=<ticket>` in the phone's browser (Google refuses sign-in inside an in-app
view). That route takes the ticket once, keeps the OAuth state and the PKCE verifier in the server's memory, sets
a browser-binding cookie (HttpOnly, Secure, SameSite=Lax, path /oauth/youtube) and redirects to Google. The
callback needs the unexpired state (used up by its first use) and the same browser's cookie before it exchanges
the code; the refresh token goes to the radio's Keychain item. The redirect URI comes from `panel_base_url`
only, never from the Host header. Replies are no-store and no-referrer; nothing of the flow is logged; signing
in creates nothing on YouTube. Both routes answer 503 while mirroring is off.

### Session usage prices

The panel estimates API-equivalent cost; it does not measure subscription billing. Built-in prices are limited defaults and may be out of date. Set `usage_prices` in the existing JSON configuration to override or extend them. Each key is a lowercase model ID prefix, and each value contains exactly four finite numbers from 0 to 1,000,000,000 in USD per million tokens: input, output, cache write, cache read. Numbers below are examples only, not actual prices:

```json
{"usage_prices": {"claude-opus-5-5": [1, 2, 3, 4]}}
```

Configured prefixes take precedence over defaults; the longest matching configured prefix wins. A prefix matches the entire ID or a hyphen-delimited beginning, so it also covers dated variants. Rates must be verified and maintained by the user. Models without a configured or built-in price show “no price set”; the totals show how many tokens have no price. A displayed partial estimate excludes those tokens.

The built-in model prices are estimates: prices as of 2026-11-03.
