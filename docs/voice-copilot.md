# The Tower: voice co-pilot, protocol v1

The Tower is an optional Hebrew voice pipe for the authenticated owner. It reads
agent status and queues spoken messages to a chosen live session. It cannot run
commands, change settings, answer decisions, grant approvals, or call arbitrary
bridge APIs. The owner approved this direction; native iOS calling
is a separate slice.

## Opt-in and deployment

`panel_settings.voice_copilot` defaults to **false** (no `voice-copilot-on` state
marker). Settings → Radio and sharing contains the switch. When enabled,
Cockpit v2 → Tools → **The Tower / המגדל** opens the conversation page. Turning
it off rejects every Tower endpoint and ends an active upstream connection.

Install the optional, pinned `voice-copilot` extra with the repository's locked
dependency workflow. The core bridge remains standard-library only. Missing
WebSocket support, missing Vertex configuration or invalid credentials fail
closed; there is no provider or paid fallback. No cloud project is provisioned.

The transport adopts the established BSD-licensed `websockets` synchronous
client rather than implementing RFC 6455 inside the panel. Version 15.0.1
supports the bridge's Python 3.9 floor. Its universal wheel and source hashes
are recorded from the [PyPI release](https://pypi.org/project/websockets/15.0.1/);
`uv lock --check --offline` verifies lock consistency. Installing and checking
the actual package still requires registry access in the operator environment.

Configuration (normal bridge config, never supplied by the phone):

| Key | Default | Purpose |
| --- | --- | --- |
| `gemini_vertex_project` | existing radio configuration | Existing Vertex project |
| `gemini_token_command` | existing radio configuration | Existing radio access-token helper |
| `voice_copilot_model` | `gemini-live-2.5-flash-native-audio` | Supported native-audio Live model |
| `voice_copilot_location` | `us-central1` | Regional Vertex endpoint, not `global` |
| `voice_copilot_daily_minutes` | `90` | UTC daily wall-clock cap; integer 0–1440 |

The model is configurable to support future upgrades without changing the phone
protocol. Model availability and pricing require live operator verification.
The feasibility report's prices, latency, credits and iOS entitlement claims
are estimates, not guarantees or authorization to buy anything. This slice
uses the established server-side Vertex WebSocket API, not ephemeral client
Google credentials. See Google's Live API reference (Vertex AI documentation)
and 2.5 native audio model (Vertex AI documentation).

## Owner HTTP protocol

All requests use the existing panel HTTPS origin and full signed Telegram
`initData` in **`X-Telegram-Init-Data`**. The panel validates the Telegram HMAC,
owner ID, timestamp and mirroring policy, just as it does for other owner-only
APIs. Never put initData, a Google token or a call ID into a URL. App-scoped
tokens and widget credentials do not authorize these routes.

Opening a paid call requires the normal action-fresh login (15 minutes).
Audio/events/stop use the normal read-fresh owner login (one hour) and an
unguessable call ID from that start. This allows a call to reach its 30-minute
hard limit without turning speech into an approval channel. A call ID alone
never authenticates a request. One active conversation and one event consumer
are allowed per panel server.

Every request body is a JSON object with **only** the listed fields:

| POST path | Body | Response |
| --- | --- | --- |
| `/api/voice-copilot/start` | `{}` | `{ok:true, session:"<32 hex>", protocol:1}` |
| `/api/voice-copilot/audio` | `{session, data}` | `{ok:true}` |
| `/api/voice-copilot/events` | `{session}` | streamed `text/event-stream` |
| `/api/voice-copilot/stop` | `{session}` | `{ok:true}` |

`audio.data` is base64 **raw mono PCM16, little endian, 16,000 Hz**. Each frame
is at most 16,000 bytes (500 ms); 80–150 ms is preferable. Do not upload WAV
headers, compressed media, video, text, model configuration, tool calls or tool
results. Upload sequentially; on a slow connection stop rather than building a
speech backlog. There is no automatic retry of audio or start.
The server replenishes upload credit at 32,000 PCM bytes per wall-clock second
(one audio second per second), with at most one second of burst credit. It also
limits each conversation to 57,600,000 uploaded bytes (30 minutes of PCM).
Accounting is atomic across concurrent HTTP uploads; exceeding either limit
rejects the frame without forwarding it and closes the conversation.

The events request uses streaming `fetch` with the auth header (not browser
`EventSource`, which cannot set it). The existing threaded HTTP panel streams
one JSON event per SSE record: `data: <JSON>\n\n`. Proxy buffering must be off;
the server sets `X-Accel-Buffering: no`. No browser WebSocket upgrade or new
listener is needed. Heartbeats every two seconds keep the stream active.

| Event `type` | Additional fields / action |
| --- | --- |
| `ready` | `input_rate:16000, output_rate:24000, max_seconds:1800`; start microphone uploads |
| `audio` | `data` base64 raw mono PCM16, `rate:24000`; schedule contiguous playback |
| `transcript` | `speaker:"owner"\|"tower", text`; append the transcription fragment |
| `turn_complete` | End the current transcript paragraph |
| `interrupted` | Immediately cancel queued output audio (barge-in) |
| `relay` | `queued:boolean`; confirmation of queuing only, never proof of delivery |
| `heartbeat` | No action |
| `closed` | `reason, message`; Hebrew visible/spoken explanation; release microphone/audio |

Errors are JSON `{ok:false,error,message}`. Authentication fails with the
panel's bare 403. Disabled/busy/closed return 409, malformed input 400, daily
cap 429, configuration/transport/ledger failures 503. Terminal stream errors
are `closed` events. Hebrew messages can be spoken with local device speech
when no paid session exists. Tokens, raw transport exceptions and wire dumps
are never returned or logged.

## Tools and approval boundary

Only the server's four declarations are sent to Google:

* `list_sessions()` reads titles and states of live owner-addressable sessions,
  plus `owner_utterance.id` for the latest completed, unrelayed owner utterance,
  or `null` if none is eligible.
* `session_status(title)` reads open progress cards, non-stale open decisions
  count and the last reply summary. It never answers an open question.
* `whats_waiting()` reads the shared owner waiting counts.
* `relay_message(session_title,utterance_id)` resolves one exact, unique live
  title and queues the matching owner's complete input transcription via
  `Inbox.put`. The model can select only the target and utterance ID; a `text`
  argument is rejected. Duplicate or missing titles require clarification.
  It never guesses a short ID or creates/reopens a session.

Only this live connection's upstream `inputTranscription` events populate the
in-memory utterance. Input must follow accepted microphone audio, and fragments
are accumulated until the transcription's own `finished:true` flag; a model
`turnComplete` does not complete an input transcription. The complete utterance
must contain 1–600 characters before sanitization; oversized speech is refused,
never truncated into a different request. A new utterance invalidates the old
ID, and a successfully queued utterance cannot be queued again under a new tool
call ID. Missing, unfinished, stale and cross-conversation IDs fail closed.
Tool results, output transcription and model-written text never populate this
registry or become session messages.

Status readers intentionally do **not** call panel `build_state`, which can
reconcile progress cards and change radio state. Read tools have no mutations.
Tool results use the panel's credential filter before transmission to Google.
Tool call IDs are deduplicated for the lifetime of the conversation.

Relays are `source:"owner", voice:true, typed:false, via:"voice co-pilot"`.
The queued text starts with `(via voice co-pilot)`. The existing
`ctb.framing.deliver` adds `VOICE_MARK` ("voice note: transcript, never an
approval") and `NEVER_APPROVAL` around its fenced content. These are **never**
`source:"panel"` items: panel messages can carry approval authority. Even a
relayed "yes" cannot qualify as owner-typed text. Queueing is the sole tool
side effect; the existing bridge inbox consumer owns delivery. No commands,
settings setters, decision handlers, session wake/reopen or public messaging
tools are exposed to the Live model.

## Duration, privacy and cleanup

`voice-copilot-usage.json` in the private state directory stores only the UTC
date and charged seconds. Five-second reservations are written atomically
under a cross-process lock **before** opening/continuing Vertex. Short unused
reservations are not refunded, including after a crash. Corrupt/unreadable or
future-dated ledgers fail closed. Midnight starts the next UTC day's budget;
reservations never cross midnight. The per-call hard cap is 30 minutes.
Renewal charges any elapsed gap after an expired reservation as well as the
next lease. If the remaining daily budget cannot cover that gap, the connection
closes before accepting more audio or tools.

The 15-second microphone inactivity timer starts only at upstream `ready`,
after microphone permission and initial connection. Startup has a separate
113-second deadline: the existing 90-second token-helper timeout, eight seconds
for opening the connection, and 15 seconds for the setup acknowledgement.
Repeated setup acknowledgements do not reset the microphone inactivity timer.

Output is bounded; slow consumers, 15 seconds without microphone uploads after ready,
transport failure, upstream `goAway`, switch-off or disconnect end the call.
There is no silent reconnect, background session or extra billing. Native Live
may terminate before the local maximum; the owner can start another call.
Stopping/reloading/leaving the Tower page releases microphone tracks and the
audio context. Hiding the browser or locking the screen ends this browser
slice. The screen must stay on.
Network sends and closure run outside the conversation state lock. Stopping
first shuts down the underlying TCP socket to interrupt blocked sends, then
closes the WebSocket; its one-second close timeout alone cannot bound waiting
for the transport's internal send mutex.

Audio and live transcripts are kept in bounded memory, not recorded. A relayed
message is deliberately persisted by the existing inbox/delivery path. Audio
processing takes place in Google Cloud using the existing project's policies.
No access token is persisted by this feature or sent to the phone; the radio's
existing in-memory token helper is reused. The WebSocket library has a private
disabled logger so debug logging cannot print authorization headers.

## Later iOS slice and verification limits

The iOS client can reuse protocol v1 after obtaining the same supported
Telegram owner login, and must never embed Google credentials. Implement native
microphone capture/resampling, PCM playback with interruption, audio-session
routing, interruption/route-change handling and lifecycle cleanup. CallKit,
background audio, Bluetooth/CarPlay, entitlement and App Review decisions,
privacy disclosures and physical-device QA remain separate work. Do not infer
native permission or entitlement approval from this browser prototype.

Offline tests use fake Vertex sockets, fake inboxes and in-memory HTTP
handlers. They cover switch/auth boundaries, approval framing, the closed
tool surface, duration/cost guards, error redaction and stream cleanup. They
cannot prove real Hebrew speech quality, Vertex model/region access, cellular
latency, proxy buffering, iPhone Telegram microphone permission or playback.
Those need an explicit owner/device verification before rollout.
