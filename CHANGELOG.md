# Changelog

All notable changes to Harnessgram are listed here. The format follows
the Keep a Changelog convention. Harnessgram is pre-1.0: a minor version (0.x) may
change a setting or a command, and the change is listed here under "Changed" or "Removed".

## [Unreleased]

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
