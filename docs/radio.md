# Team radio

A Telegram live stream in the home forum group that sounds like a radio: short spoken updates on what moved
forward, what waits for you and what needs your action, a quiet background between them, and a live summary
card as the picture. Listen from the group on your phone like any live stream. macOS only.

```
ctb radio start            # open the group's live stream (unless switched off) and start the radio
ctb radio stop             # stop it and end the live stream
ctb radio status [--live]  # JSON: on/off, mode, last update, voice minutes today (--live asks Telegram too)
ctb radio say "text"       # speak one line now
```

The same switch is in the control panel (the "Team radio" card) and in the home group as `/radio`,
`/radio on`, `/radio off` (owner only).

## Hourly channel-news edition

The radio can read an hourly, child-suitable summary of Telegram channels selected by the owner. It is off by
default. Set `news_edition_enabled` to `true` and put channel usernames in `news_edition_channels` in the
main configuration. An empty list is equivalent to off and makes no account or model call.
The list accepts at most 20 broadcast channels. `news_edition_daily_seconds` (default 300) is a news-only
paid-speech sub-cap tracked separately inside the radio's shared cap; after it is spent, editions use the local voice
without the usual paid-minutes notice. Numeric IDs work only after the account's dialog list or an existing cache
provides their access hash; usernames are the supported configuration form.

The reader reuses the radio's existing `telethon_python`, `account_api_command`, and `account_session_command`.
It only reads posts already available to that account: it never joins a channel, posts, or marks a message read.
Each run considers only the preceding hour and at most 50 posts per channel. Resolved channel IDs and access hashes are cached in an
owner-only state file. The existing bridge text-model route
is used when available; otherwise the representative post is reduced to one sentence locally. Both source text and
the final script pass the shared spoken-content suitability checker.

News editions are queued only with the private-listen speech path and its existing voice budget. They are never
mixed into the group/public stream, posted to a chat or topic, or copied to a public mirror. The scheduler permits
one edition at a time and makes at most one attempt per hour; channel names and post or summary text are not logged.

## Private Cockpit player

The Cockpit home tab has a compact Listen/Pause control, a live indicator and an off state. On iPhone it uses
native audio-only HLS, avoiding Telegram's live-video player. Tap Listen to start; pausing affects only your
player. The audio element survives Cockpit refreshes and tab changes. No player library or public stream is added.

`radio.radio_player` defaults to `true` for the home radio; set it to `false` to disable this output. The same
ffmpeg encoder tees its 48 kHz, 96 kbps AAC packets into two-second MPEG-TS segments in `radio/hls/` (directory
mode 0700), keeping six playlist entries and deleting old segments. The optional output has its own bounded,
dropping FIFO and ignores failures, so disk trouble does not block RTMP. A missing/stale output produces a
content-free diagnostic; stopping the pipeline/radio clears the files. The YouTube staff edition is unaffected.

`POST /api/radio-player` requires the usual owner's Telegram auth header. It returns availability and a
ten-minute HMAC capability scoped to this owner and the radio routes. `GET /radio/live.m3u8?token=...` signs
each relative segment URI; only that playlist and `segment-<digits>.ts` are served, with no-store responses.
Expired/forged tokens, symlinks, stale playlists and other file names are refused. Tokens cannot authorize
other Cockpit routes, and requests are not logged. Keep any front proxy's access logs disabled/redacted for
these routes too. An independent timer refreshes authorization two minutes before token expiry, with state
polls as a fallback; renewal reconnects and can briefly buffer. Temporary radio restarts resume listening
unless Pause was tapped; segments use increasing epoch-based numbers and mark discontinuities. After an
expired Telegram login, reopen the Mini App. Actual iPhone playback and
background behavior must be checked on the device; browser support alone is not device verification.

### Live title

The iPhone lock screen can show what the radio is talking about. When a spoken clip goes on the air, the station
writes its title to `radio/now.json` (src/ctb/radio_now.py): the clip's opening line after the station's greeting,
at most 80 characters. Invisible and direction characters (a soft hyphen too) are removed first, so a key split by
one is whole when the credential filter (the one everything the radio says goes through) looks at it; the cut title
is filtered again. That final text must pass the child-suitability check (src/ctb/radio_suitability.py: the shared checker, or the
portable word list; it fails closed); one that does not is never shown, and the previous title stays. A new radio
run starts without a title.

`GET /listen/<token>/now.json` answers `{"title": ..., "updated": <unix seconds>}` with the same token check as the
listen link's page and audio (a wrong or revoked token answers 403, the `listen_link` add-on off answers 404). While
the radio is off, or before anything was said, the title is the station's name and `updated` is 0. The HarnessGram
app polls it while it plays the listen link.

This is the JSON fallback, not timed ID3 metadata inside the HLS stream: the private stream's segmenter is a separate
ffmpeg process fed the encoder's FLV on standard input, and it has no input that could carry a changing `TIT2` frame
without a second muxing stage, so the title travels beside the stream instead.
## Start on Play (the Safari listen link)

You no longer have to switch the radio on before the Safari player works. **Pressing Play is the detection**: when the
private listen page (`/listen/<token>/live.m3u8`, `src/ctb/listen_link.py`) asks for the playlist and there is no fresh
audio, the bridge starts the radio itself (`src/ctb/radio_play.py`) and answers at once with a **live playlist of short
silent segments** (`src/ctb/radio_quiet.py`). AVPlayer (the iPhone app) and Safari can fail on a live playlist with no
segment, so there is always one to play: `quiet-<n>.ts` is 2.005 s of AAC-LC 48 kHz stereo silence (94 frames, the
radio's own audio format), built in memory once per number and cached (no encoder, no file, no speech, nothing paid).
The window of six segments slides with the clock (a new number every 2 seconds) and its timestamps are contiguous, so
the player keeps pulling silence until real segments exist.

**The switch to real audio.** When the radio's own playlist appears, the listen link serves it with an
`#EXT-X-DISCONTINUITY` before its first segment (the real audio is a new timeline), kept while that segment is in the
window; once it has left, `#EXT-X-DISCONTINUITY-SEQUENCE` counts it, as the HLS rules require. An encoder-written tag at
that segment is not doubled. The silence is numbered by the clock and the radio's own segments by microseconds, so the
listen link also rewrites `#EXT-X-MEDIA-SEQUENCE` of the real playlist by one fixed offset (chosen at the first real
playlist, so it continues right after the last silent number): the player sees one monotonic sequence. File names stay. The rewrite applies only to the listen link, only when silence was served in the last two
minutes, and starts afresh with each run of the radio.

- **Always private.** A Play start is the existing start with `play=True` (`ctb radio start --play`, a hidden flag): audio
  only, the private listen stream, no card, no RTMP. The account helper never runs, so a group live stream is never
  opened or ended from this path, whatever the "Live stream in the group" switch says. The owner's own start (Cockpit,
  `/radio on`, `ctb radio start`) is unchanged and still opens the group stream when that switch is on.
- **Presence is the fetches.** Every authenticated playlist or segment response records its time
  (`player-listener.json`, no URL, token or address). Every private station stops after
  `private_idle_minutes` (default **10**) of continuously detected absence. The stop clears the saved intent,
  follows the regular stop path and sends one owner-chat notice. When detection is unavailable or uncertain,
  the interval resets and the station stays on. Group live stations are not stopped by this rule.
- **A queued start checks again.** The start runs in its own process, a moment after the request. Under the radio's
  control lock it re-checks that nothing stopped the radio since the request, the add-on and the listening link are still
  on, and the cap is not spent; otherwise it does nothing (`radio_play_skipped`).
- **The idle stop is atomic with fetches.** The decision rechecks the station, the current listener and fetches
  since the interval began under the presence and control locks. A new station or listener cancels the stop.
- **Guards.** A valid listening token, the `listen_link` add-on on, the radio configured with its player allowed, and
  mirroring on. At most one start attempt per 60 seconds (a fetch inside that window gets the "starting" answer and
  starts nothing). No start when today's paid-speech cap for listening (`tts_listening_minutes`) is spent or the usage
  ledger is unreadable; the old "not found" answer is served instead (a cap of 0 means paid speech is off, which costs
  nothing, so it does not block). No start while the radio is wanted by the owner (it resumes by itself), or while a
  start or stop is already in progress. A wrong token is refused as before (403) and starts nothing. The segment route
  never starts anything. Nothing is logged with the token or the URL.
- **Settings** (in the `radio` object): `play_start` (default `true`) controls starting on Play;
  `private_idle_minutes` (default `10`) controls the private station's idle stop.
- `ctb radio status` shows `play_started`.

Playback of the silence and of the switch should be confirmed once on a real iPhone, in Safari and in the app's player.

## Group live stream (optional)

The add-on switch "Live stream in the group" (`radio_group_live`, Cockpit Settings, default **on**) decides whether the
radio also opens the Telegram live stream in the home group. The private listen stream (Cockpit player, native
HarnessGram player, Safari link) is always made.

**The switch is read only when the radio starts.** `ctb radio start` (and the resume after a restart) reads it once and
records the result as the radio's mode in `state.json`; the running radio keeps that mode. Flipping the switch while the
radio runs changes nothing until the next start: the Cockpit card shows "The change applies the next time the radio
starts" while the switch and the running mode differ. (Stop and start the radio to apply it.)

| | Switch on at the start (default) | Switch off at the start |
|---|---|---|
| Live stream in the group | opened with your account, RTMP published (unchanged) | not opened: no `ensure`, no `end`, no stream key read, no account use |
| Picture (card, project show) | drawn and encoded | none: the encoder is audio-only |
| Private listen stream (HLS) | made as before | made, and always on (`radio_player` is ignored) |
| YouTube mirror edition | works as before | works the same |
| `ctb radio status` / Cockpit card | `mode`: `group+private` | `mode`: `private` |
| Telegram settings needed to start | the account and RTMP commands | none |

- **Audio-only mode.** One encoder process reads the radio's mixed audio and writes the same AAC (96 kbps, 48 kHz) as
  FLV to the private stream's segmenter. There is no picture input, no card or board painter, no JPEG/PNG frames, no
  video encoder, no RTMPS connection and no stream key (`LocalSink` in `radio_rtmp.py` replaces the publisher). Against
  the group mode this removes a 1280x720 15 fps H.264 encode (libx264 `veryfast`, or VideoToolbox), the 2 to 10 fps frame
  decode and scale, the Swift card renderer on each change and the project show's painter, which is most of the radio's CPU
  and memory; what is left is a stereo AAC encode (a percent or two of one core).
- **The YouTube mirror edition** reads the separate station-speech HLS output (`public-hls`) and draws its own picture.
  With the group stream off, a local audio-only encoder supplies that speech output. Private library music and
  episodes never enter the group or mirror source.
- **A stream recorded earlier.** With the switch off, start never touches the account: a live stream the radio recorded
  in an earlier run stays recorded exactly as it is, and only `ctb radio stop` ends it (and never anyone else's).
- **Listener detection** skips Telegram's participant count while the group stream is off; only the Cockpit/Safari
  player's fetches count, so the paid voice runs only while someone listens there. The player setting is computed from
  the mode (never stored): with the group stream off the private stream is always served, and a later start with the
  switch on goes back to the configured `radio_player`.

## How it works

- **Installed-code handover (macOS).** The heartbeat compares the radio's recorded running build with the
  installed build. A stale, still-running radio is kickstarted only when the offline `radio youtube status`
  reports both `on: false` and `broadcast: false`, with no audible private episode, active room, or
  playing/paused/downloading podcast item. Unknown status waits. Each installed build gets at most one
  attempt, persisted before launchctl runs, with a 15-minute cooldown between builds; a failed attempt
  stays visible to `doctor` instead of looping. A live YouTube broadcast is left alone. During the gap,
  the authenticated player can use a fresh legacy `radio/hls` feed only while the recorded radio process
  is known to run older code. A fresh `private-hls` feed takes precedence; segments never fall back
  independently from a fresh private playlist. Both paths retain the same file and playlist guards.
- **It comes back after a restart.** `start` records that the radio is wanted (`radio/wanted.json`); only `stop`
  (or the radio giving up after repeated failures) clears it, never a shutdown. When the listener starts and the
  radio is wanted but its job is gone (the Mac was restarted), the listener starts it again once, after a short
  wait, with backoff and a limit on failed attempts. A radio that was running is never restarted, and the YouTube
  staff edition is never resumed: going live there stays the owner's own action.
- **The live stream.** A bot cannot start a group live stream, so `start` uses your own Telegram account
  through a small Telethon script (`src/ctb/radio_call.py`). It runs under a separate Python that has Telethon,
  receives only the home group's id, and can only read the group's active call, create an RTMP live stream,
  read its state and end it. It never starts a stream in any other chat, and it ends only the stream the radio
  recorded.
- **The stream key never leaves memory.** The RTMP URL and key come from two configured commands (for example
  Keychain reads). ffmpeg only encodes: it writes FLV to a pipe, and `radio_rtmp.py` publishes that FLV over
  RTMPS from Python. So the key is never on a command line (`ps -ww` shows none), in a file or in a log.
- **One background job, only while on.** `start` loads the launchd job `com.claude-telegram-bridge.radio`;
  `stop` unloads it, stops ffmpeg and ends the live stream. If the encoder or the connection keeps failing
  (5 times in 10 minutes) the job ends the live stream and unloads itself.
- **What it says.** Every 30 seconds it looks at the control panel's state (decisions, reminders, sessions,
  quota), sessions waiting on an open question, the optional owner's ledger folder and the optional quota
  command. Something new waiting for you is spoken at once; other changes at most every `every_minutes`;
  when nothing changed it stays quiet. The words come from a template (`radio_script.py`); numbers are spoken
  as words. An optional `rewrite_command` can polish the periodic updates (it must answer between `<<<` and
  `>>>`; anything else falls back to the template).
- **The voice.** Gemini TTS on Vertex (your `gemini_vertex_project` and `gemini_token_command`), cached by
  exact text, with a daily cap while listening (`tts_listening_minutes`, default 60 minutes of paid speech).
  A listener is detected when the Cockpit player fetched a signed HLS playlist or segment within 90 seconds,
  or Telegram's live stream has at least one participant. Telegram is checked at most once a minute in a
  separate worker; status and Cockpit requests only read the cache. Failed or stale checks mean unknown
  and do not authorize paid speech. The private player state contains only the last fetch timestamp; the
  call cache contains only a check timestamp and an aggregate participant count (or null on failure).
  The default `voice_route` is `local`: the configured offline `tts_command` is used for all narration, with the
  system voice (`say_voice`, Carmit for Hebrew) as fallback. Set `voice_route` to `cloud` to explicitly opt in with
  your own credentials. In cloud mode, a listener permits Vertex, followed on failure or an exhausted daily cap by
  Cloud TTS `he-IL-Chirp3-HD-<radio voice>`, `he-IL-Wavenet-B`, the offline engine, and finally the system voice.
  The Cloud routes use the same project and access token as Vertex. Rooms share the paid budget,
  with an additional smaller room sub-cap,
  and also require listening.
  On installations with neither the player nor account status configured, `tts_daily_minutes` remains the
  fallback cap (default 25); the example below sets it to 0. Existing configuration is never rewritten.
  `ctb radio status` and Cockpit Settings show today's premium usage, the listening cap and listening source.
  Usage remains counted per local day; a listener reconnecting does not reset it.
  The local cap is a safeguard: each request first reserves at most twice its estimated length (and asks the model for no
  more audio than that), runs only when the reservation fits in what is left today, and is then counted at what
  was billed (the audio token count or the audio length, whichever is more). A request whose result is unknown
  (a timeout) stays counted at its full reservation and blocks further paid requests that day. Only a billing
  limit actually enforced by the provider can guarantee an absolute spending ceiling; a budget alert alone cannot.

## Private music and podcasts

The optional **Private listening music** add-on is off by default. When enabled, the DJ selects only library
items that passed the radio's suitability checks, favours liked tracks, excludes disliked tracks, and does not
repeat a track within 24 hours. Owner-selected podcast episodes use the same fail-closed gate and play to the end.
Station speech has priority: it immediately ducks music and temporarily interrupts an episode only for an urgent
line. Like and skip controls, the current title, and the add-on switch are on the Cockpit radio card.

Copyright isolation is structural. Music and episodes enter `PrivateMixer` and the `private-hls` encoder only.
The group stream, staff edition, and YouTube mirror receive `SpeechMixer` through the separate `public-hls` path;
that mixer ignores every background/show source. The existing private title endpoint also receives track and
episode titles for the iPhone lock screen. This adds no encoder or long-running media process.

**בקצרה:** המוזיקה והפודקאסטים כבויים כברירת מחדל, עוברים בדיקת התאמה, ומתנגנים רק בקישור ההאזנה הפרטי.
הדיבור קודם להם תמיד; אפשר לסמן אהבתי, לדלג ולכבות בכרטיס הרדיו.

## Configuration

Add a `"radio"` object to `config.json`. The radio stays off until it is configured.

```json
"radio": {
  "telethon_python": "/path/to/venv-with-telethon/bin/python",
  "account_api_command": ["/usr/bin/security", "find-generic-password", "-s", "my.telegram", "-a", "api", "-w"],
  "account_session_command": ["/usr/bin/security", "find-generic-password", "-s", "my.telegram", "-a", "session", "-w"],
  "rtmp_url_command": ["/usr/bin/security", "find-generic-password", "-s", "my.radio", "-a", "rtmp-url", "-w"],
  "rtmp_key_command": ["/usr/bin/security", "find-generic-password", "-s", "my.radio", "-a", "rtmp-key", "-w"],
  "voice_route": "local",
  "tts_model": "gemini-2.5-flash-tts",
  "voice": "Sadaltager",
  "tts_listening_minutes": 60,
  "tts_daily_minutes": 0,
  "every_minutes": 20,
  "ledger_dir": "~/.local/state/my-ledger",
  "quota_command": ["/path/to/quota-script"],
  "project_names": {"my-app": "My app"}
}
```

| Key | Default | Meaning |
|---|---|---|
| `telethon_python` | (required) | A Python with Telethon, for `radio_call.py` |
| `account_api_command`, `account_session_command` | (required) | Print the account's `api_id:api_hash` and its Telethon session string |
| `rtmp_url_command`, `rtmp_key_command` | (required) | Print the group's RTMP server URL and stream key |
| `voice_route` | `local` | `local` uses the offline `tts_command`; `cloud` explicitly opts in to Vertex and Cloud TTS, with local fallback |
| `tts_model`, `voice`, `style` | `gemini-2.5-flash-tts`, `Sadaltager`, a calm radio host | The paid voice |
| `tts_listening_minutes` | 60 | Daily cap of paid speech while a listener is detected; 0 = no new paid speech |
| `tts_daily_minutes` | 25 | Fallback cap only when listening detection is unavailable (player disabled and account status unconfigured); 0 = no new paid speech |
| `say_voice` | Carmit for Hebrew | The free system voice |
| `every_minutes` | 20 | Least time between updates that are not urgent |
| `bed`, `bed_db` | `off`, -30 | Background between updates: `off` is true digital silence (the default); `pad` is an optional soft chord drone, about -45 dBFS RMS at -30 |
| `ledger_dir` | none | Folder of `<project>.json` ledgers (`items` with `status`, `wait`, `updated`) |
| `quota_command` | none | Prints lines like `Codex: 5h 79%`; run every 5 minutes |
| `project_names` | none | Spoken names of the ledger projects |
| `rewrite_command` | none | Optional: reads a brief on stdin, prints the script between `<<<` and `>>>` |
| `path` | none | Extra folders for the job's PATH (for example where `gcloud` lives); the PATH of the shell that ran `start` is added too |
| `video_encoder` | VideoToolbox, else libx264 | The H.264 encoder for the card |
| `motion_modules`, `chrome_path` | none, the installed Chrome | Only for `radio prepare-motion` (see the flight board) |
| `room_tts_daily_minutes` | 15 | The brainstorming room's daily sub-cap inside the shared paid-speech budget; at most 15, 0 = the free voice only |
| `room_suitability_file` | none | Optional: a JSON file with a `pattern`; a room line it matches is never said or shown, and a configured file that cannot be read keeps rooms closed |

Files live in `<state>/radio/` (private): `state.json`, `status.json`, `last.json`, `radio.log`, the voice cache
and the daily voice usage. None of them holds a key or a token.

## The project show (off by default)

Instead of the summary card, the picture can be a moving show of every active project. Turn it on or off with
the "Project show on the radio" switch in the control panel's radio settings; the stream picks the change up
within a second (it reconnects once, with the other picture).

- **The slides.** First an overview: one glowing column per project, with its name, a progress ring showing
  done/total, its state, its crew and what waits for you (up to five in a row, eight in two rows, then pages).
  Then a wall per project, most recently active first: three columns of paper notes (done, in work, next up),
  each finished note checked. When there is little to show, one combined wall holds every project's notes.
  Last, your desk: what waits for your answer, oldest first, shown only when something waits. A news ticker
  runs along the bottom of every slide: what landed in the last hour, then what waits for you.
- **Where the notes come from.** Per project: the owner's ledger (`ledger_dir`), every topic's pin-board
  checklist and reminders, progress cards (each finished step keeps the time it was reached), the sessions and
  their running background agents, open decisions and open questions. A topic in a project group belongs to
  that project; a home-group session by its title prefix. Done items older than a day are not shown. Every
  text passes the bridge's credential filter before it is drawn.
- **Light on the machine.** Each slide is drawn once when what it shows changes (a small Swift painter,
  compiled once like the card's renderer); between changes the same picture is sent again. The ticker is drawn
  once per change as a long strip. Its window advances every `radio_show.TICKER_STEP_FRAMES` input frames
  (default 3), keeping the same average scroll speed with fewer changed pictures. The show's JPEG input is
  10 fps; the output is constant 15 fps by default (`CTB_RADIO_OUT_FPS`), with closed two-second GOPs and no
  B-frames. Both encoders use H.264 Main level 3.1, even dimensions and limited-range yuv420p. The board's
  target and maximum video rate are both 600 kbps with a 1200 kbit VBV buffer; VideoToolbox enables constant
  bitrate and libx264 signals CBR HRD. The PNG card's encoder arguments, AAC audio and isolated HLS player
  process are unchanged. Encoder checks do not replace a live Telegram iPhone playback check.
- **Motion units, prepared once.** `harnessgram radio prepare-motion --modules <folder with
  node_modules/puppeteer-core> [--chrome <program>]` renders the station ident and the "landed" sting into
  `<state>/radio/motion/`. They are never rendered while streaming. When something new lands, the sting plays,
  then that project's wall comes in and the fresh note's check stamps on. Without the units the slides still
  move in.
- **Falls back by itself.** If drawing fails, the card is shown again and the show is tried after ten minutes.
- **The staff edition** shows its own board when the switch is on, built only from the staff view: each listed
  project's staff label, a progress bar with done/total for each live progress card whose visibility is
  `public` (private and team cards never count), the percent, the day's counts. Numbers and the label only: no
  item or card text, no walls, no ticker.

## Brainstorming room (off by default)

A live round-table discussion on the owner's radio ("השולחן העגול", stage 1). Turn it on with the "Brainstorm
room on the radio" switch in the control panel's radio settings; turning it off ends a live room.

- **Opening a room.** On the radio card, pick a topic that waits for you (an open decision, a reminder, an open
  task) or type your own, pick 5, 10 or 15 minutes and tap "open the room". The radio must be on.
- **The table.** A moderator and fixed seats: the critic (who must object in every round), the designer, the
  CEO (who decides last), and a delegate for each project the topic names (at most two). A delegate is given a
  short digest of its project's state (done, in work, next, waiting on you) and nothing else, and says so when
  the digest does not cover a question. Three rounds: ideas, objections, decision. Each seat has its own paid
  voice; the moderator uses the radio's voice. While a room is live the routine news and the sessions' spoken
  replies wait, and the picture is a text card: who is around the table and who speaks, the round, and up to
  three proposals (stage 2 draws the table).
- **Joining.** A voice note (or a text) in the room's own topic in the home group joins before the next speaker,
  as the owner's words, never as an approval. "רגע" ("one moment") in the Cockpit pauses the room: every room line
  not yet on the air is dropped (whether being written, synthesised or queued) and nothing new starts until you
  speak, type a line in the Cockpit, tap "go on", or four minutes pass. It cannot stop the one sentence already on
  the air (the mixer never cuts a clip), and a text or voice request already sent is still billed.
- **The end.** A summary card in the Cockpit: the decision, up to four proposed tasks and the open questions.
  Nothing is sent anywhere by itself. "Send to session" on a task queues it to the session you pick as a held
  proposal: it is delivered at the end of that session's current step (a running session is never interrupted)
  and is framed as a button tap, which never approves a sensitive action.
- **Safety.** The bridge's off switch, the room switch, stopping the radio (or the radio's job ending) each end a
  live room for good; switching on again never resumes it, and a restarted station never carries on a room of an
  earlier run. Every room line is checked when its voice is ready (in one step with queueing it, so a switch-off
  can never fall between) and again when it would start playing, so a line of an ended or paused room is never
  said. The summary allows at most two model calls (a malformed answer is dropped field by field), within a
  per-room call budget; past it the room sums up locally from its proposals. The room exists only in the owner's radio: the staff edition on YouTube never reads it,
  and its lines are not posted to the alerts channel. Every word the room says or shows passes one filter first
  (credentials removed, plus- and cross-like signs replaced, one reading direction per line, the optional
  suitability rule file, a length limit), and every request to the text model goes through the outgoing filter.
- **Cost.** All paid speech uses one daily budget in `<state>/radio/tts-usage.json`. Room speech also counts
  against a sub-cap (`room_tts_daily_minutes`, default and ceiling 15 minutes) in the same ledger. Both caps are
  checked and reserved under one file lock and saved in one atomic write, so concurrent requests and restarts
  cannot reuse reserved minutes. Legacy room usage is imported once and included in both counters.
  When either cap is used up the room goes on in the free system voice. Every paid request first carries a length
  ceiling (`maxOutputTokens`). A model that refuses it (HTTP 400, not billed) is asked once more without it and is
  remembered; the length is then held by the text: speech seconds are estimated from the characters (0.09 s each,
  deliberately slower than the measured 0.075), text beyond about 40 estimated seconds is split at sentence ends
  into several requests, each reserved and settled before the next, and a text whose requests do not all fit the
  remaining paid minutes goes whole to the free voice (never half and half). If a provider exceeds its
  reservation anyway, its full observed cost is recorded, its audio is discarded without caching, and subsequent
  requests use the free voice until the ledger is reviewed and its blocked field cleared. An unknown result or
  crash leaves the request pending and blocks further paid requests for that day. Only one request per day may be
  pending across processes; an older unresolved request does not block a new day and could still be running.
  The provider can charge beyond the requested output limit, including when a response is lost. A local cap
  cannot prevent or undo that charge; only a provider-enforced billing limit can guarantee a spending ceiling.
  A damaged shared or legacy room ledger disables paid speech until repaired or restored; it is never overwritten.
  Cloud TTS uses a separate locked `<state>/radio/tts-free-usage.json` ledger with UTC-month hard ceilings of
  900,000 Chirp3-HD characters and 3,800,000 WaveNet characters. A request reserves its characters before it is
  sent, and a malformed ledger disables these routes without overwriting it. These ceilings deliberately stay
  below Google's free allowances; after either ceiling, speech continues through the next fallback.
  Requests completing past midnight settle on their reservation date, without reducing the new day's usage.
  Usage is kept per date, so moving the clock to another day and back, or backwards, never frees minutes
  already spent. A 10-minute room speaks about 6 minutes: roughly $0.09 of Gemini 2.5 Flash
  speech and $0.03 of Gemini text at list prices (an estimate, not a measurement); 15 minutes of speech costs
  about $0.23 at the estimated rate, subject to provider billing. Repeated fixed lines come from the voice cache for free.

Files live in `<state>/radio/room/` (private): `room.json` (the room, its lines and the owner's words),
`topic.json` (the room topic's chat and thread). Paid voice usage is kept in the shared radio ledger above.

## YouTube mirror edition (explicit standing owner start)

The mirror is for a maintainer who wants the unlisted YouTube copy to carry the full home radio (same
summaries and voice, plus the moving flight board) whenever the radio is on. It starts following only after
an explicit "mirror on" from the owner, and "mirror off" ends it.

`radio.youtube.edition` accepts `"staff"` (the default, unchanged) or `"mirror"`. A missing or invalid value
selects staff. **Mirror publishes the full home radio content**, including the owner's summaries and voice;
the staff allowlist does not apply. It uses the existing connected YouTube channel, reusable ingest stream,
unlisted broadcast settings and Keychain credentials. It adds no voice calls or separate narration.

```
harnessgram radio youtube mirror on     # explicit standing owner start; select mirror and enable it
harnessgram radio start                  # home radio and its armed mirror follow the same lifecycle
harnessgram radio stop                   # stops both; standing mirror permission remains for the next start
harnessgram radio youtube mirror off    # stop the mirror and revoke its standing owner start
```

The Cockpit also offers **Enable standing mirror**, followed by a second confirmation naming the channel and
explicitly covering all home content whenever the radio runs. **Stop mirror and revoke permission** works
even with the bridge off. These actions run the same CLI implementation. Status includes `edition` and
`mirror_owner_start`; the Cockpit shows the mirror's content description rather than the staff privacy claim.
The explicit action selects `mirror` and enables it through `cockpit.json`; these saved choices override the
configuration file, just like the existing feature switch. To return to staff, turn the mirror off,
then enable the safely disabled staff edition with the usual feature switch. `mirror off` selects staff again
and turns its feature switch off; it never starts a staff broadcast.

Only `mirror on` writes `<state>/radio-youtube/mirror-start.json`, recording the owner's standing start,
approval date, channel and connection generation. Configuring `edition: "mirror"`, starting the home radio,
reboot/resume, a scheduled job or ordinary YouTube `start` never grants that permission. Changing/reconnecting
the channel invalidates it; the owner must explicitly enable the mirror again. Home start/resume only loads
an already-authorized mirror job. Turning the mirror off revokes permission before attempting network cleanup.

Audio comes from the isolated station-speech `<state>/radio/public-hls/live.m3u8`, enabled by `radio.radio_player`.
The private listening playlist (`radio/hls/live.m3u8`) is never used as a fallback. A separate speech mixer ignores
background/show assignments; the private mixer cannot be attached to a publishing pipeline. Routing does not depend
on an expiring private-audio marker, so transitions and stale markers cannot leak licensed audio.
The mirror waits quietly while that playlist is absent, stale, ended, or the home radio/player is off; it
creates no broadcast during the wait. ffmpeg reads the local live edge at real-time pace. Bounded playlist
reloads and pipeline reconnects handle disappearing/restarted playlists. Losing the home feed ends the
YouTube broadcast; when a fresh live feed returns the existing standing start permits a replacement.

The picture uses the same `radio_board.gather` / `collect` and `radio_show.BoardShow` renderer as the home
flight board, with separate private drawing files. It reads only cards whose visibility is exactly `public`
(`gather(public_only=True)`; see "Agent cards" in docs/panel.md): private and team cards stay on the owner's own
board. The room's spoken digest on the home audio reads public cards only as well, always: a mirror can be armed
mid-room, after a digest was cached, so no private card text ever enters the room. The board and scrolling ticker are encoded at a steady
25 fps, 2-second keyframes and a 1.8 Mbps video target/max rate with a 3.6 Mb VBV buffer. Prepared motion units
are reused from the home radio. The mirror never calls the staff script, voice or card. Telegram retains its
existing still-card setting; enabling the mirror never changes `radio-board-on`.

The bridge's `policy.on()` switch stops publication and ends the current mirror broadcast, regardless of
the standing start. While it remains off no broadcast is created. A still-authorized mirror waits and may
resume after the bridge and home HLS return; staff still requires a new explicit `start`. Home stop ends the
mirror job, while mirror errors or revocation never stop or reconfigure Telegram. Broadcasts rotate at
`max_hours` while the mirror is authorized and the home feed remains live. Repeated actual pipeline failures
use the existing bounded retry budget and stop only the YouTube job. Credentials stay in Keychain/in memory:
ffmpeg only sees a local playlist, image pipes and FLV on stdout, never ingest addresses or stream keys.

## Podcast queue (off until you add a feed or a link)

Play an episode on the private listening stream. Station speech starts immediately and ducks the episode underneath.

- **Sources.** Your feed list, `radio_podcasts` in `config.json` (a top-level key, default empty):
  `"radio_podcasts": [{"name": "Science Hour", "url": "https://example.org/feed.xml"}]`, and single episode links
  (an audio file, or a feed, whose latest episode is taken).
- **Adding an episode.** In the Cockpit's radio area ("Podcast on the radio"): open "Add an episode", pick a feed,
  pick an episode; or paste a link. In the home group, `/podcast <feed name or link>` queues the latest episode;
  `/podcast` alone says what is playing; `/podcast play | pause | skip | stop` are the same controls as the
  buttons. The queue holds at most 10 episodes; stop empties it.
- **On the air.** The Cockpit shows a "now playing" line and play/pause, skip and stop. While an episode plays the
  private stream mixes speech over it immediately, lowering the episode to -18 dB over 0.5 seconds and restoring
  it over 1 second after speech. Pause keeps the episode's place. A live brainstorming room holds new episode
  starts; bridge-off stops playback.
- **Reading a feed safely.** https only, port 443, no login in the address; at most 2 MB and 12 seconds; at most
  3 redirects, each checked again; every address a name resolves to must be public, checked on the connection that
  is used (a loopback, private, link-local or carrier-grade address is refused); no proxy; a feed with a DTD or
  entity declaration is refused. An episode comes only from a feed you configured or a link you pasted, never from
  an audio address the page sends.
- **Suitability.** Before an episode is queued, its title and description (a pasted audio link: its file name) pass
  the same rule file as the brainstorming room (`radio.room_suitability_file`). An episode marked explicit is never
  queued, and a configured rule file that cannot be read refuses every episode. An item whose feed or page gives no
  title and no description is refused (a file name is not text to judge). The check runs again just before each
  episode is played, so a changed rule applies to what is already queued. The shared checker reads Hebrew; text with
  Latin letters must also pass the portable word list in `radio_suitability` (English words), and text in any other
  script is refused, because no rule covers it.
- **Private cache.** The head of the queue is downloaded into `<state>/radio/podcast/cache/` (private, at most
  150 MB an episode, 20 minutes), decoded by ffmpeg, and deleted when it ends. The cache is pruned to 400 MB and
  two days, by exact file name only.
- **Private stream only.** Episodes play only on the private listen stream, never the Telegram group or either
  YouTube edition. Episode titles are never spoken, put on the card or sent to the alerts channel.

## Private gap filler (Issue #407)

`ctb radio music on|off` controls all private background playback (default on); the same switch is in the
Cockpit radio settings. Without speech, eligible library tracks use the existing likes/skips weighting and
exclude the last ten picks. An exhausted or empty library stays silent. Configure optional owner-chosen RSS feeds:

```sh
ctb radio podcast add https://feeds.example.org/program.xml
ctb radio podcast list
ctb radio podcast remove https://feeds.example.org/program.xml
```

Saved feed choices live in private state, without changing the bridge config; removing a configured feed suppresses
it too. Every five minutes, a bounded background reader selects the newest unplayed suitable episode across those
feeds. Only HTTPS audio enclosures qualify. Existing download limits, redirect/DNS protection and title/description
checks apply again at queueing and playback. Completed episodes are recorded in `radio/podcast/played.json`.

The station fans out only synthesized broadcast PCM to a separate speech queue. The private mixer alone reads library
and episode decoders. Speech begins on its next 20 ms frame, ducks the background to -18 dB over 0.5 s, and restores it
over 1 s afterward; PCM addition saturates safely. Mixing happens before the existing ffmpeg AAC/HLS encoder. The
group and mirror read only the speech queue and `public-hls`; there is no private-audio fallback.

The filler reuses the morning briefing's Shabbat and configured quiet-day windows. During those windows, when the
switch is off, or if its quiet-day check fails, music and episodes are muted and stopped. Station speech retains its
existing policy. No live-service restart or publication is part of installing these source changes.

### Agent media (add-on `radio_agent_media`, default on)

Agents can put an audio or video file they made in the private radio queue.

- `ctb radio queue-file <path> --title T [--session S] [--text-file F]` resolves the path first, accepts only a
  regular media file under the home folder (at most 500 MB), and binds approval to its size, time and SHA-256.
- The complete title and transcript/captions pass the radio suitability check, which fails closed. Without text,
  the owner receives a “Play on the radio?” card and only their tap queues it.
- Mirrored audio and video carry an “On the radio” button. `ctb mirror --file F --radio-auto --radio-text-file T`
  queues automatically only during car mode or while the owner is listening, and only after text passes.
- The radio announces “Next: &lt;title&gt;, from &lt;project&gt;” before playing it. The podcast controls pause and skip it.
  Agent media uses the podcast player's isolated private mixer; `public-hls` and the YouTube mirror contain only
  station speech and never receive the file. Guards and the fingerprint are checked again immediately before play.

## Staff edition on YouTube (off by default)

An unlisted YouTube live stream the owner can share with his staff. It is a **separate radio**, not a copy of
the owner's: the owner's radio reads his decisions, questions, quotas and every project; the staff edition is
built from an allowlist and never sees any of that.

```
harnessgram radio youtube status [--live]   # JSON; works offline (--live also asks YouTube)
harnessgram radio youtube connect           # sign in with Google and pick the channel
harnessgram radio youtube start             # go live: the owner's own action (a publication)
harnessgram radio youtube stop              # stop and end the broadcast
harnessgram radio youtube disconnect        # revoke the sign-in and remove the stored keys
```

```json
"radio": {
  "youtube": {
    "enabled": false,
    "edition": "staff",
    "projects": {"my-app": "My app"},
    "sections": ["station_id", "done_today", "in_work"],
    "deny_terms_file": "~/.config/harnessgram/private-terms.txt",
    "suitability_file": "~/.config/harnessgram/suitability.json",
    "client_command": ["/bin/cat", "/path/to/google-oauth-desktop-client.json"],
    "max_hours": 11
  }
}
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Nothing runs and `start` refuses until this is `true` |
| `edition` | `"staff"` | `"staff"` keeps the independent allowlisted station; `"mirror"` needs the explicit standing owner start described above |
| `projects` | `{}` (nothing) | Ledger project key → the label staff hear and see. Only these projects are ever read. A project picked in the Cockpit without a label here is shown as a neutral "Project A". Empty = the station only identifies itself |
| `sections` | all three | Subset of the fixed list `station_id` (always on), `done_today`, `in_work` |
| `private_idle_minutes` | `10` | Stop private broadcasts after this many continuous minutes without a detected listener. When listening detection is unavailable, broadcasts stay on. |
| `title` | `שידורי הקוקפיט` | The station's name. Used only when both lists below are in place and it passes them (and the team names); else the built-in name. The same title goes on screen, in the voice and on the YouTube broadcast |
| `deny_terms_file` | the private deny-list | One PLAIN term per line (`#` comments), compared case-, space- and punctuation-insensitively. A line with regular-expression syntax is refused: `start` says so and a running broadcast ends until it is fixed |
| `suitability_file` | the radio's `room_suitability_file` | A JSON object with a child-suitability `"pattern"` |
| `client_command` | none | Prints a Google OAuth client file (`installed` or `web`) or `{client_id, client_secret}` |
| `keychain_service` | `harnessgram.radio-youtube` | Keychain service of the refresh token, ingest URL and stream key |
| `max_hours` | 11 | The broadcast ends after this long (YouTube archives only streams shorter than 12 hours); going live again is a new `start` |

What the staff hear and see: a station id (the station name and the time), and for each listed project its staff
label (or a neutral "Project A"; never a key, topic name or session title), its live progress cards (the bridge's
`ctb card progress`) as a bar, a percent and done/total steps each, and counts in words: how many items it finished
today (spoken once, while fresh) and how many are in work. Spoken lines say the label, the percent and the counts.

Structured data only: no free text from a card (its title, its step names) or from a model ever reaches the
screen, the voice or YouTube; the station keeps a card's counts and nothing else. A card belongs to a project only
when it lives in that project's own group chat and the group's name is the listed key (an ambiguous key gets no
card); a card in the owner's home chat, or one whose topic title merely names a project, never counts. Every card
the board draws is counted in the spoken line, and nothing else. Narration is fixed templates filled with these
numbers. The station title and the staff labels pass the deny-list, every part of each team member's name (split on
spaces, hyphens and other marks) and the suitability list, on the same normal form; a label that fails becomes
"Project A", a title that fails (or any title while a list is missing) becomes the built-in name.
Ledger item titles are never read either (a plain sentence can be private). Never: decisions or questions, items waiting on the owner,
quotas, costs, accounts, session ids, file paths, links, error text, or anything from a project not listed.
Every sentence and card line must consist of plain words of the station's language (Hebrew: no Latin letters
and no digits, numbers are words) and pass the outgoing-text sanitizer unchanged; anything else is dropped.

The job reads the settings again on every round and before every reconnect: `enabled: false` ends the broadcast
at once, a project taken off the list is gone from the next update, and the broadcast ends `max_hours` after the
owner's start even across restarts of the job. Before it connects, and whenever the team's state cannot be read,
the card is replaced by an identification-only card, so a card from an earlier broadcast is never on air.

How it runs: its own launchd job (`com.claude-telegram-bridge.radio-youtube`), its own card, its own encoder
(the same settings and the same hardware encoder as the radio) and its own RTMPS connection, so a YouTube problem
never touches the Telegram radio. Failures restart the pipeline with a growing pause (up to 60 s); 5 failures in
15 minutes end the broadcast and unload the job. It speaks with the free local voice only, so it never spends the
paid voice minutes. The background is the radio's own synthesized pad (no third-party music).

The bridge's off switch (`"mirror": false`) applies here too, read again on every round: while off, `start`
refuses (nothing is created on YouTube) and `connect` asks for no new sign-in; turning it off while live stops
the stream within about a second and ends the broadcast. Turning the bridge on again does not resume it: going
live again is a new `start`. `status`, `stop` and `disconnect` keep working while off.

Broadcasts that could not be ended are kept in a cleanup list (`status` counts them) and finished first by the
next `start`, `stop` or `disconnect`; an id is dropped only once YouTube confirms.

Going live: only `start` creates a broadcast (unlisted, not made for kids, auto start and stop, no monitor
stream) on a reusable ingest stream, then loads the job. The job streams only to a broadcast an owner `start`
recorded; starting the Telegram radio never starts this one. The ingest URL and the stream key are fetched
through the API into the Keychain (written on `security -i`'s stdin, never on a command line) and read into
memory by the job. They never reach ffmpeg at all: ffmpeg encodes to a pipe and the job publishes over RTMPS
itself, so `ps` shows neither (no environment variable or temporary file holds them either). YouTube must have live streaming enabled on the channel first (YouTube Studio; the first
time can take up to 24 hours); `connect` and `status --live` report it.

Sign-in: `connect` uses Google's installed-app flow (loopback redirect with PKCE, scope `youtube.force-ssl`). On
the Mac the browser returns by itself; from a phone, 127.0.0.1 does not open, so paste the address the browser
shows. A Google OAuth client in "Testing" status issues refresh tokens that expire after 7 days: publish the
consent screen ("In production") for a lasting connection.

The Cockpit card ("Staff radio on YouTube", in the agents view) does all of this from the phone: "Connect with
YouTube", the feature switch (off by default), the project picker (none by default), a two-tap "Go live" whose
confirmation names the channel, "Stop" and "Disconnect", with the state (not connected, connected as the
channel, live since a time with the hours left, ended, bridge off, live streaming not enabled on the channel).
The switch and the picked projects are kept in `<state>/radio-youtube/cockpit.json` and override the config
file's `enabled` and `projects`. The card's sign-in is a web OAuth flow that returns to the Cockpit's own public
address (see docs/panel.md); the CLI's loopback sign-in stays.

Who publishes is checked at the last moment. Every change of the connection (a sign-in, a disconnect) and
Start's last step run under one lock and move a connection generation. A sign-in is stored by one function only,
which needs the generation from before the sign-in opened, refuses while a broadcast runs or an owner start is
recorded, and writes in a crash-safe order (a new generation marked not connected, then the credential, then
connected). Start, under the lock, asks Google which channel the credential that will publish belongs to and
creates nothing unless it is the confirmed channel (the Cockpit's request, or the stored connection for a CLI
start). A Cockpit start passes `--request <id>`: one 0600 file per request, taken once by an atomic rename, valid
2 minutes; a missing, used or expired request refuses. The running job ends its broadcast when the generation
changes, and disconnecting while live ends the broadcast first, with the credential that started it.

One-time setup for the Cockpit sign-in (a developer step, done once):
1. `panel_base_url` in config.json: the Cockpit's public https origin (scheme, host and port only).
2. A Google OAuth client of type "Web application" whose authorized redirect URI is
   `<panel_base_url>/oauth/youtube/callback`, with the YouTube Data API enabled in its project.
3. Its id and secret in the Keychain, next to the radio's own items (service `harnessgram.radio-youtube`, or the
   configured `keychain_service`), accounts `web-client-id` and `web-client-secret`. Until both exist the card
   says setup is pending.

### Stream diagnostics

For a bounded capture at the start of a connection, set `CTB_RADIO_DIAG_SECONDS` in the radio process environment
to a positive whole number (maximum 120). It is off by default. After the window, a background thread writes
one uniquely named `radio-diag-*.json` file per connection in the radio state folder, with owner-only permissions.
The file contains timing and size metadata, never media data. Capture is limited to 100,000 timing rows;
samples may also be dropped under contention. A send still in progress at the end of the window has no end time
in the report. Run `ctb radio diag-report <path-to-json>` to summarize FLV audio gaps, read bursts, slow sends,
and audio/video writer lag. Remove the environment setting to disable it.

## Voice digest

Cockpit **Settings → Voice digest** controls deterministic voice-note batches of the
radio's existing spoken replies. No additional editor or model session runs.
The top-level `voice_digest` configuration accepts:

| Value | Delivery |
| --- | --- |
| `off` | No collection or delivery |
| `important` (default) | Important items only; at most once per hour |
| `hourly` | All nontrivial items; at most once per hour |
| `every_3_hours` | All nontrivial items; at most once every three hours |

The first interval starts at the first eligible reply; later intervals start at
the previous delivery. Empty batches never send. Importance is computed from the
source reply before shortening: a nonempty `Summary:` other than “nothing new”,
“no updates”, “still working” or an acknowledgement, **or** an explicit decision,
job completion or error in the body (English/Hebrew keywords in `voice_digest.py`).
This is a deterministic signal, not a claim that arbitrary prose is understood.
The existing `radio_talk.trivial` gate still discards acknowledgements and short
source replies. The stored importance flag is independent of spoken phrasing.

The running radio's `on_reply` writes the same sanitized record for the live
station and a durable digest consumer under `radio/digest/pending/`; consuming a
live reply cannot remove the digest's input. Only replies collected while the
radio and its reply setting permit narration are included. This extends the
existing spool, TTS cache, shared daily TTS budget, local voice fallback and
heartbeat service rather than introducing another scheduler or speech service.

Every heartbeat round admits at most one digest worker; a nonblocking file lock
also excludes other processes. The worker merges whole spoken items into a
caption of at most 900 characters, retains overflow for the next interval,
converts radio PCM to OGG/Opus with ffmpeg, and sends exactly one `sendVoice` to
`home_chat(cfg)`, without a thread override (General in a forum). The caption is
the same scrubbed bulletin passed to TTS. Mirroring off suppresses collection
and delivery; settings are read again before sending.

All levels are silent from **23:00 through 07:00 in the host's local timezone**,
including daylight-saving changes. There is no calendar dependency in the
bridge: `voice_digest_quiet_windows` supplies weekly Sabbath and dated holiday
windows. Its default conservatively blocks Friday noon through Sunday midnight.
Add local holiday start/end windows ahead of time; holidays are **not** inferred
automatically. Weekly endpoints are `mon 07:00` through `sun 23:59`; dated
endpoints use local ISO datetimes. Starts are inclusive, ends exclusive, and
weekly windows may wrap into the next week. Invalid windows suppress delivery.
For example (replace the dated window with your local holiday times):

```json
{
  "voice_digest": "important",
  "voice_digest_quiet_windows": [
    {"start": "fri 12:00", "end": "sun 00:00"},
    {"start": "2027-04-21T16:00", "end": "2027-04-23T21:00"}
  ]
}
```

Delivery state is persisted before the network request and tombstoned before
input cleanup, so normal restarts never repeat delivered items. Generation failures
and confirmed Telegram API rejections (4xx) retry next heartbeat round, then drop
with one content-free warning. Sends use the bridge's shared optional flood gate;
a locally skipped send keeps the batch without spending a retry. An ambiguous
in-flight send (including timeout, lost response, server failure or crash) is consumed without replay: Telegram
has no idempotency key for `sendVoice`, so preventing duplicates can lose a
batch whose acceptance could not be confirmed. Nothing in this mechanism
changes publication or account permissions.

Radio privacy is unconditional, even with `redact_outgoing: false`: full source
text, names, session labels and metadata are checked before formatting and
before either spool write. Email addresses and their local parts, known account
identifiers, labelled account IDs, long numeric IDs, UUIDs and credential-looking
tokens are scrubbed. Structured private fields and already loaded configuration
values also identify short secrets wherever they recur in the source or output.
Unknown, unlabelled opaque values cannot reliably be recognized as secrets.
Metadata is restricted to the existing timing fields and
safe flags; filenames contain generated identifiers. The same boundary runs
again before digest TTS/caption delivery. Regression tests read the actual JSON
spool with outgoing redaction disabled, and verify the delivered caption.

Final spoken replies (including the plain fallback) and digest text pass an
offline child-suitability gate before narration or delivery. When installed,
`~/.agents/content-suitability/check.py` is called locally with a private temporary
file; rejected text and checker failures never send. Hosts without a callable
checker use a conservative local word list. This lexical check does not establish
semantic suitability for arbitrary prose.

## The listen button

Separate from the radio: a 🔊 button sits under each final Hebrew reply in a topic (never under alerts, cards,
decisions or progress messages). The owner taps it and gets a voice note of that one reply, sent as a reply to it.
Config key `listen_button` (default `true`) turns it off. On the first reply of a topic, which carries the fixed
 keyboard, the button is added by an edit; if Telegram refuses that, it follows in a short message of its own.

- The spoken text is the Hebrew text the topic shows, without code blocks, links, file paths and the Summary label,
  at most 1200 characters (a cut text ends with "the rest is in the topic"). It passes the same suitability rules as
  the radio's spoken lines (`room_suitability_file`); text they refuse, or rules that cannot be read, are not spoken.
  With no rule file the shared offline child-suitability gate (`radio_suitability`) still runs, and fails closed.
- The voice is local and free. By default it is the system voice (Carmit, converted to OGG/Opus). With
  `tts_command` set (an argv list, no shell) the bridge runs that local engine instead: the text goes in on stdin,
  `-o <file.ogg>` names the output, and `-v <tts_voice>` is added when `tts_voice` is set. The engine runs in its
  own process group with a 90-second limit, its output and errors are never logged or sent, and a result that is not
  OGG is dropped. When it fails, the system voice speaks instead. The paid voice is never used for this button.
- One recording is made at a time and at most 20 an hour. The recording's Telegram file id is kept per reply, so a
  second tap sends the same voice note again (after the same text checks, off the update loop; the id is dropped only when
  Telegram says the file is gone). Anyone but the owner gets an "owner only" answer.
- The button's id is a short random token in `<state>/listen.json`, which keeps the shown text of the last 300 replies
  (private, like the other state files).
