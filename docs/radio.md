# Team radio

A Telegram live stream in the home forum group that sounds like a radio: short spoken updates on what moved
forward, what waits for you and what needs your action, a quiet background between them, and a live summary
card as the picture. Listen from the group on your phone like any live stream. macOS only.

```
ctb radio start            # open the group's live stream and start the radio
ctb radio stop             # stop it and end the live stream
ctb radio status [--live]  # JSON: on/off, last update, voice minutes today (--live asks Telegram too)
ctb radio say "text"       # speak one line now
```

The same switch is in the control panel (the "Team radio" card) and in the home group as `/radio`,
`/radio on`, `/radio off` (owner only).

## How it works

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
  exact text, with a daily cap (`tts_daily_minutes`, default 25 minutes of speech). Over the cap, or when Vertex
  fails, it uses the system voice (`say_voice`, Carmit for Hebrew), which is local and free.
  The cap is hard: each request first reserves at most twice its estimated length (and asks the model for no
  more audio than that), runs only when the reservation fits in what is left today, and is then counted at what
  was billed (the audio token count or the audio length, whichever is more). A request whose result is unknown
  (a timeout) stays counted at its full reservation. For a ceiling on money as well, set a budget or a quota on
  the Google Cloud project itself; the bridge cannot see the bill.

## Configuration

Add a `"radio"` object to `config.json`. The radio stays off until it is configured.

```json
"radio": {
  "telethon_python": "/path/to/venv-with-telethon/bin/python",
  "account_api_command": ["/usr/bin/security", "find-generic-password", "-s", "my.telegram", "-a", "api", "-w"],
  "account_session_command": ["/usr/bin/security", "find-generic-password", "-s", "my.telegram", "-a", "session", "-w"],
  "rtmp_url_command": ["/usr/bin/security", "find-generic-password", "-s", "my.radio", "-a", "rtmp-url", "-w"],
  "rtmp_key_command": ["/usr/bin/security", "find-generic-password", "-s", "my.radio", "-a", "rtmp-key", "-w"],
  "tts_model": "gemini-2.5-flash-tts",
  "voice": "Sadaltager",
  "tts_daily_minutes": 25,
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
| `tts_model`, `voice`, `style` | `gemini-2.5-flash-tts`, `Sadaltager`, a calm radio host | The paid voice |
| `tts_daily_minutes` | 25 | Daily cap of paid speech; 0 = system voice only |
| `say_voice` | Carmit for Hebrew | The free system voice |
| `every_minutes` | 20 | Least time between updates that are not urgent |
| `bed`, `bed_db` | `pad`, -30 | Background between updates (`pad` or `silence`) and its level |
| `ledger_dir` | none | Folder of `<project>.json` ledgers (`items` with `status`, `wait`, `updated`) |
| `quota_command` | none | Prints lines like `Codex: 5h 79%`; run every 5 minutes |
| `project_names` | none | Spoken names of the ledger projects |
| `rewrite_command` | none | Optional: reads a brief on stdin, prints the script between `<<<` and `>>>` |
| `path` | none | Extra folders for the job's PATH (for example where `gcloud` lives); the PATH of the shell that ran `start` is added too |
| `video_encoder` | VideoToolbox, else libx264 | The H.264 encoder for the card |
| `motion_modules`, `chrome_path` | none, the installed Chrome | Only for `radio prepare-motion` (see the flight board) |
| `room_tts_daily_minutes` | 15 | The brainstorming room's own daily cap of paid speech, counted apart from `tts_daily_minutes`; at most 15, 0 = the free voice only |
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
  once per change as a long strip, and the encoder moves a window along it, so it scrolls smoothly without any
  drawing. The stream runs at 10 frames a second (JPEG frames, hardware H.264 at 600 kbps) only while the show
  is on.
- **Motion units, prepared once.** `harnessgram radio prepare-motion --modules <folder with
  node_modules/puppeteer-core> [--chrome <program>]` renders the station ident and the "landed" sting into
  `<state>/radio/motion/`. They are never rendered while streaming. When something new lands, the sting plays,
  then that project's wall comes in and the fresh note's check stamps on. Without the units the slides still
  move in.
- **Falls back by itself.** If drawing fails, the card is shown again and the show is tried after ten minutes.
- **The staff edition** shows its own overview when the switch is on, built only from the staff view:
  allowlisted project names and two counts (done today, in work), never an item's text, no walls, no ticker.

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
- **Cost.** The room speaks under its own daily cap (`room_tts_daily_minutes`, default and ceiling 15 minutes),
  counted in `<state>/radio/room/tts-usage.json`, apart from the radio's 25 minutes; when it is used up the room
  goes on in the free system voice. The room's requests always carry a length ceiling: a voice model that refuses
  one is never asked without it (the room then uses the free voice). Usage is kept per date, so moving the clock
  to another day and back, or backwards, never frees minutes already spent. A 10-minute room speaks about 6 minutes: roughly $0.09 of Gemini 2.5 Flash
  speech and $0.03 of Gemini text at list prices (an estimate, not a measurement); 15 minutes a day is at most
  about $0.23. Repeated fixed lines come from the voice cache for free.

Files live in `<state>/radio/room/` (private): `room.json` (the room, its lines and the owner's words),
`topic.json` (the room topic's chat and thread) and the room's voice usage.

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
    "projects": {"my-app": "My app"},
    "sections": ["station_id", "done_today", "in_work"],
    "client_command": ["/bin/cat", "/path/to/google-oauth-desktop-client.json"],
    "max_hours": 11
  }
}
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Nothing runs and `start` refuses until this is `true` |
| `projects` | `{}` (nothing) | Ledger project key → the name staff hear. Only these projects are ever read. Empty = the station only identifies itself |
| `sections` | all three | Subset of the fixed list `station_id` (always on), `done_today`, `in_work` |
| `client_command` | none | Prints a Google OAuth client file (`installed` or `web`) or `{client_id, client_secret}` |
| `keychain_service` | `harnessgram.radio-youtube` | Keychain service of the refresh token, ingest URL and stream key |
| `max_hours` | 11 | The broadcast ends after this long (YouTube archives only streams shorter than 12 hours); going live again is a new `start` |

What the staff hear and see: a station id (the station name and the time), and for each listed project its staff
name with counts in words: how many items it finished today (spoken once, while fresh) and how many are in work.
No item text is ever read, not even the listed projects' own titles (a plain sentence can be private). Never: decisions or questions, items waiting on the owner,
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
