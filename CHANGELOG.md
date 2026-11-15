# Changelog

All notable changes to Harnessgram are listed here. The format follows
the Keep a Changelog convention. Harnessgram is pre-1.0: a minor version (0.x) may
change a setting or a command, and the change is listed here under "Changed" or "Removed".

## [Unreleased]

## [0.4.0] - 2026-11-16

### Added
- Claude Code permission prompts can be answered from the phone. A new PermissionRequest hook (`ctb hook-permission`)
  posts the exact request (the whole tool input, every field) to the session's topic and the Cockpit and waits for
  the owner's answer; whoever answers first, the phone or the computer, wins. No is a button; Yes is a typed yes
  sent as a reply to that card, or the Cockpit. A request the phone cannot show exactly can only be declined. No
  answer leaves the terminal prompt as it was; nothing is allowed by default and no permission rule is added. Run
  `harnessgram install` (or tap Restore in the hook-health notice) to add the hook. See docs/panel.md.
- Sensitive-button markers for the Harnessgram iPhone app: every inline button that performs a sensitive action by
  itself (adding, removing or switching accounts, moving a session to another account, team members, deleting a topic forever, the alerts channel's publication settings, and agent or
  dialog options whose label names a sensitive area) leaves with `hg!` in front of its callback data, which the bridge
  strips before any handler reads it; a URL button that leads to a sensitive answer gets a "🔐 " title. The app asks
  for Face ID before acting on a marked button; every handler keeps its own checks. Each decision in `/api/state` now
  carries `"sensitive": true|false`. See docs/panel.md, "Widget devices".
- Widget device API: `POST /api/device-pair` (fresh owner login) returns a random 30-day device token, stored only as
  a hash and limited to 5 pairings an hour; `POST /api/devices` lists and revokes paired devices, also from a new
  Settings row; `POST /api/widget-state` (read-only) and `POST /api/widget-decide` take only
  `Authorization: Bearer <device token>`. A widget answers only a decision its asker marked routine with the new
  `ctb decide ask --routine` (default off; old records are never routine) whose words name nothing sensitive; anything
  else is `403 {"error": "needs-app"}`. Answering the same way twice is a success. See docs/panel.md, "Widget devices".
- Radio live title: `GET /listen/<token>/now.json` answers `{"title", "updated"}` (the same token check as the listen
  link), naming what the radio is saying now in at most 80 child-suitability-checked characters, for the iPhone lock
  screen. See docs/radio.md, "Live title".
- The radio starts when you press Play in the Safari listen page, so you no longer switch it on first. The first
  playlist request of the private listen link, with no radio audio yet, starts the radio in private audio-only mode
  (never a group stream, never the YouTube mirror) and answers with a live playlist of short silent segments (built in
  memory, nothing paid) that slides until the first real segment exists, then switches over with an
  EXT-X-DISCONTINUITY, so AVPlayer and Safari both keep playing. A station started this way stops by itself after 10 minutes
  (`radio.play_idle_minutes`) with no listener fetch; a radio you started yourself is never stopped by this. Guarded by
  the token, the add-on, one attempt per minute and the daily paid-speech cap; `radio.play_start: false` turns it off.
  See docs/radio.md.
- Choice cards with pictures reach agents better: the hint every agent gets now says to attach one `--image` per
  option whenever the options are visual, and a choice card published without images prints a one-line tip. In the
  Cockpit the option tiles sit in two columns (one under 360 px), with lazy-loaded images that open large on tap
  without answering. See docs/panel.md.

- The radio's group live stream is optional: the new add-on switch `radio_group_live` ("Live stream in the group",
  default on, so nothing changes until you switch it). Off, the radio opens no live stream in the group and publishes no
  RTMP, and runs audio-only (no card, no project show, no video encoding) feeding only the private listen stream that the
  Cockpit player and the Safari link play. `radio status` and the Cockpit radio card show the mode ("group + private" or
  "private only"). The switch is read when the radio starts and a change applies at the next start. See
  docs/radio.md.
- Per-project **work map** (Cockpit v2 project page, a pinned message in each project's lead topic, and the owner's
  `/what`) and a **morning briefing** (a "Good morning" card on Now, a text in the home chat and, when the radio is on,
  the private home stream; `/briefing` sends it now). Agents add `impact: ...` notes to ledger items so each line says what
  it changes for the owner. Both are add-ons (`work_map`, `morning_briefing`) in the add-ons screen; `briefing.time` sets the wake time; `/briefing` works in the home chat only. See docs/panel.md.
- Voice echo: with "Confirm voice notes" on, an owner recording that goes in without a transcript preview (car mode, or a
  preview that could not be sent) gets one quiet reply with what was heard. Forwards are never echoed. Switch:
  the `voice_echo` add-on (default on).
- "Listen in Safari": a one-tap button on the Cockpit radio tile opens a minimal listening page in Safari, which keeps
  the team radio playing in the background and on the lock screen. The page and its audio are authorized by a private,
  unguessable listening token (kept only as a hash, revoked in one tap from Settings) that reaches the page, the
  playlist and the segments, never an API; it expires after 30 days and follows the radio player switch. New add-on switch `listen_link` (default on). See docs/addons.md.
- Cockpit v2: a "Request an app" tile (Tools, More) and the owner's `/app <link>` command send one App Store link to the
  command set in the new `app_request_command` setting (default empty = hidden) and show the answer. See docs/panel.md.
- "What the system knows" (behind the new Cockpit switch): one private index of the agents' rules, memories,
  skills, tools and docs, with on/off switches, locked safety rules, one-tap undo and change requests for the team.
  Switched-off items reach the agents through a generated overlay file; no source file is rewritten
  (`harnessgram knowledge`, docs/knowledge.md).
- Cockpit v2: a "Mac maintenance" tile (Tools, More): a cleanup check, a cleanup that needs a fresh check and a second tap,
  and the weekly routine, run as background jobs from the argv lists in the new `mac_tools` setting (default empty =
  hidden). See docs/panel.md.

- Cockpit v2: a "My schedule" card on Now (Today and Tomorrow, one-tap done and undo) for the owner's own items, added
  from a one-field sheet or with the owner's `/todo <text>` command (Hebrew and English day and time words; anything
  unclear stays undated). New settings `timezone` (default Asia/Jerusalem) and `schedule_sources` (default empty: an
  optional read-only private .ics link or file merged into the card). See docs/panel.md.
- "My schedule": a one-tap "Connect Google" button shows the owner's Google Calendar events on the card, read only
  (OAuth web sign-in with PKCE through `panel_base_url`, refresh token in the Keychain, one-tap Disconnect that also
  revokes it). New setting `google_calendar` (default empty = hidden) and `harnessgram schedule google-setup-info`,
  which prints the redirect URI to register. See docs/panel.md.

### Fixed
- `harnessgram install` and `uninstall` no longer drop the owner's own hook when it shares a hook group with a
  bridge hook: only the bridge's entries are replaced or removed.
- The project page header in the Cockpit v2 is legible on any picture: a solid scrim on the text box itself (so it covers the text at any width; mirrored in
  RTL), a soft text shadow and per-theme inks that keep 4.5:1 contrast, guarded by a test.
- A turn's final answer no longer goes missing when the bridge is reinstalled while the turn ends. Each turn's
  delivery is claimed under the session's Stop lock before its first send. A worker whose module failed to import
  before anything went out retries with its own claim. The heartbeat (once a minute at most) redelivers a recent
  turn that no sender ever claimed (its worker never ran). A claimed turn that never completed is never resent; it
  gets one short "the last answer may be incomplete" notice.

## [0.3.0] - 2026-11-14

### Added

- Cockpit radio player: a private, audio-only Listen/Pause control on the home tab (native HLS from the radio
  encoder, short-lived signed links that grant no other Cockpit access). On by default for the home radio
  (`radio.radio_player`).
- YouTube mirror edition, off by default: the unlisted YouTube copy can carry the full home radio, but only after
  an explicit `harnessgram radio youtube mirror on`; `mirror off` revokes it.
- Fork a session in one tap from the topic (More > Fork session) or its Cockpit card: a new session with the
  conversation so far, in its own topic, while the original keeps running.
- Hook health: if a bridge hook disappears from Claude Code's settings, the owner gets one alert with a one-tap
  Restore (hooks-only merge with a backup).
- `harnessgram sessions reload [--all | --session ID] [--dry-run]` and a Cockpit action restart idle sessions
  that still run an older hook snapshot, in their own panes; busy sessions are skipped, never interrupted.
- Cockpit Requests checklist: a read-only list of items from a configured JSON ledger folder
  (`brief.ledger_dir`), waiting-on-you first, per project.
- Progress cards show the session's latest tool activity (tool and file name only; inputs are never stored).
- Optional Tailscale Funnel repair for the Cockpit (`panel_funnel_port`, default off), reported by `doctor`.

### Changed

- The Cockpit is compact by default: one-line summaries (cost, tokens, cache) that expand on tap, per-model
  blocks nested, empty rows hidden, and the open state remembered per viewer.
- One back stack for the Telegram BackButton, the iOS edge swipe and Android back.
- Reminders always have a clear button; old ones fold into an "Older" group with a one-tap restore, and a
  conservative daily sweep removes only reminders with one explicit past date.

### Fixed

- Nothing in the Cockpit runs past the screen edge at phone widths (button rows wrap, long values wrap).
- A refresh requested while the Cockpit is loading is no longer dropped.
- Cookie headers are redacted wherever channel output is redacted.
- The radio player's segmenter queue is bounded by bytes, so a short hiccup cannot fill it.

### Known limits

- Radio player and back-gesture behaviour are verified in browsers; actual iPhone and Android Telegram playback
  and gesture delivery still need checking on a device.
- The team radio remains macOS only, with Hebrew-only spoken replies.

## [0.2.0] - 2026-11-12

### Added

- The Cockpit has a bottom tab bar with icons, badges and a More sheet for less common destinations.
- Studio can show real data through a trusted local adapter with a bounded public data contract.
- Experimental, off by default: attached Codex sessions can be mirrored into their own topics through the shared harness boundary.
- A blocking command offers a Move to background button when the terminal supports it.
- Progress cards update themselves with reply summaries, the current task, background jobs and a reminder when work looks stale.
- Experimental, off by default: private chats can show decision widgets on supported Telegram clients.
- Optional radio diagnostics and outgoing-stream taps make live listening checks possible without changing the default setup.
- The Cockpit shows token use and estimated cost for each session.

### Changed

- The radio sends a steady 15 fps stream with two-second keyframes and a jitter buffer instead of releasing encoder bursts.
- Radio bulletins say what each active topic is doing, identify waits and summarize completed work without speaking email addresses or account identifiers.
- Tap answers belong to one exact recorded question, can be used only once and cannot answer money, messaging or repository questions.
- Cockpit task cards refresh faster, remind you about long work and present translated tasks and results as complete sentences.
- Radio narration uses clean whole sentences, natural pauses and digital silence between updates, with safer handling of dates and fractions.
- Setup accepts a complete BotFather message or clipboard paste and clears copied tokens when it exits.
- Usage totals use current configurable prices, distinguish cache costs and normalize model families consistently across sessions.

### Fixed

- Reply delivery retries temporary failures, keeps every pending part in a durable outbox and prevents an accepted part from being sent twice.
- Deleted or uneditable pinned dashboards are replaced without duplicates, and already-removed pins no longer cause an error.
- Linux service names are escaped safely, radio shutdown cannot prevent uninstall, and service status detection is more reliable.
- Studio serves media only from its verified descriptor, limits adapter memory and diagnostics, and keeps sentence marks stable.
- Cockpit controls remain usable while chat data loads, and frames, notices and the More sheet layer correctly above the bottom bar.
- Radio readers do not stall when queues fill or a sender stops, and delayed audio is bounded so narration stays current.
- Progress cards keep their last real outcome when a later summary has no news and count jobs and visible replies for the correct session.
- Alerts without a reply link open the session topic or group instead of the bot chat.
- Usage refreshes no longer deadlock, and Cockpit message and tool-call totals avoid duplicate or blocked counting.
- Topic names, bidirectional brackets and Telegram service notices render without misleading renames or reversed punctuation.
- A topic's reply keyboard has one source of truth. The pre-redesign layout builder is removed, every sender
  (replies, the question follow-up, the status card and its repost, /keyboard, the wake and card refreshes)
  takes the layout from one function, and a layout change is detected by itself: a card, label signature or
  "already got the keyboard" mark kept for an older layout is redrawn once, so a phone never keeps or gets
  back a keyboard of an older layout.

## [0.1.0]

The first public release. Harnessgram was developed privately before this version, so this entry describes
what the first release does rather than a diff from earlier work.

### What it does

- Mirrors every Claude Code reply and file into one Telegram topic per session, silently, through Claude Code
  hooks (no model tokens are spent on the bridge itself).
- Lets you answer from your phone in a session's topic: text, voice notes (transcribed on your machine with
  whisper.cpp), pictures and files reach the session after the current tool call, at the end of the step, with
  the next prompt, or by waking an idle session.
- Optional "reply in English, read it in your language" translation, with offline engines (TranslateGemma on
  Apple silicon, NLLB-200) or your own Gemini key; the bot's buttons and menus are in English or Hebrew.
- A quota dashboard per Claude account (pinned message and footer), an optional automatic move to another
  account at a plan limit, and a reset bank for saved quota resets.
- Sessions from Telegram: start, resume and close sessions, pick the model, compact a long session, and see a
  session's real terminal screen read-only.
- `harnessgram setup` (terminal or `--web`), `harnessgram doctor`, `harnessgram install` and
  `harnessgram uninstall` for the listener, the scheduled jobs and the Claude Code hooks, on macOS
  (LaunchAgents) and Linux (user systemd units).
- Optional, off until configured: the Cockpit (a Telegram Mini App with a "for me" list, agent cards and
  settings), project groups, a team group, an alerts channel, large files through a local Bot API server, and
  the team radio (macOS only; spoken replies are Hebrew only for now).

### Security and privacy

- Only the paired Telegram user id is obeyed; forwarded content, other users and old messages are ignored.
- `"mirror": false` turns the whole bridge off, and one outbound filter redacts credentials in text that
  leaves the machine for a model, the Cockpit or a file name. Redaction is best effort and is not a promise
  to catch every secret.
- The approval rule given to the model is guidance, not a sandbox: Claude Code's own permission system is the
  enforcement layer. See `SECURITY.md` for the threat model and how to report a vulnerability.

### Known limits

- Native Windows is not supported (the code needs `fcntl` and `pwd`); use WSL, which is treated as Linux and is
  not tested separately.
- The team radio is macOS only and its spoken replies are Hebrew only.
- The bot's menus are English and Hebrew; translation of replies supports any language the chosen engine does.
- A hand-written configuration without `target_language` reads as Hebrew (kept for older installs);
  `harnessgram setup` always writes it (English by default).
- The Cockpit setup is experimental: no installer or doctor check yet, you keep `harnessgram panel serve`
  running and provide an HTTPS address yourself.
- Quota reset times assume the Asia/Jerusalem time zone when the reset line carries no time zone.
- Defaults to know before you rely on them: the automatic move to another Claude account at a plan limit is on
  until you switch it off (account menu); the live progress card is on (`progress`); two behaviours have no
  off-switch yet: typing a "continue" prompt into a paused session when its quota returns, and reopening the
  sessions that were open after a reboot. Topic names that follow the task use Gemini only when you configure
  Gemini credentials (at most 150 requests a day, `topic_names_daily_calls`).
- Tested only with the Claude Code versions in the recorded test fixtures; a Claude Code update can change a
  terminal screen the bridge reads. Run `harnessgram doctor` after updating Claude Code.
