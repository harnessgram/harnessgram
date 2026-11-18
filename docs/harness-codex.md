# Codex bridge

The bridge now contains an opt-in app-server client alongside the persisted
observer. Set `codex_harness_enabled: true` to enable either integration. The
default remains off, and the observer described below keeps its existing
behaviour.

## App-server sessions

Run `harnessgram codex start [--cwd DIR] [--model MODEL] [--effort EFFORT]`
with `codex_harness_enabled: true` and mirroring enabled. The command starts or reuses a private
app-server for the active `CODEX_HOME`, registers the new thread and prints
a shell-quoted `codex --remote unix://… resume THREAD_ID` command for its TUI.
The listener subscribes to registered threads and creates one topic per thread
in the paired home chat. If Telegram has topic creation disabled, the bridge
waits for a topic and never posts the thread's content to General. The stored
observer skips registered app-server threads, so the two transports cannot
forward the same thread.

The socket is owner-only (`0600`) under a private directory. On systems with
short Unix socket paths, the bridge uses a short owner-private directory under
`/tmp`. A startup lock serializes reuse and replacement. A refused connection
confirms an orphaned socket. A responding socket is reused, and an endpoint
that fails the handshake or stops answering RPCs is disconnected and retried
without removing its live socket. Shutdown closes the listener's connection
and leaves the detached app-server running, so other clients and the next
start reuse it.

The client uses the Codex CLI 0.169.0 generated v2 schema and the
[app-server reference](https://github.com/openai/codex/tree/main/codex-rs/app-server).
`--effort` sets `config.model_reasoning_effort` on the thread and is sent as
`effort` on subsequent Telegram-started turns. The runtime model and effort
appear in the Cockpit alongside the thread's state and reported token usage.

## Telegram controls

- Send ordinary text as the paired owner in a registered topic. The bridge
  starts an idle thread with `turn/start` and steers an active one with
  `turn/steer` and its exact `expectedTurnId`. Steer failures never fall back
  to starting a turn. The turn's progress message offers **Interrupt turn**,
  bound to that turn. Send `/compact` in the same topic to request compaction.
  When a queued message or `/compact` fails, the bridge replies to it: "not
  delivered" after a refusal, "may not have been delivered" after a timeout or
  a lost connection.
- The existing topic indicator shows activity and the progress message shows
  when Codex begins streaming a reply. Final assistant messages go through the
  existing reply mirror with the Codex label. Commentary is opt-in through
  `codex_commentary`. Tool output and reasoning are excluded.
- Command, file-change and permission requests use the existing dialog card
  styling. Reply directly to the current card with the option number to answer
  a permission request. Sensitive and incomplete approvals require the native
  Codex client for a grant. MCP elicitation cards can decline or cancel, with
  forms and URL flows handled in Codex.
- User-input cards offer the request's choices. Reply directly to the card for
  free text when offered. Multiple questions are submitted together. Secret
  questions require the native client. No prompt is approved by the bridge.
  Card actions are bound to the paired owner, chat, topic, posted message,
  connection and pending request. Resolution, an answered request, turn
  completion and disconnect all invalidate its cards.
- Cockpit lists the native sessions with model, effort, token usage and live
  state. Its open, interrupt and answer links open the corresponding Telegram
  topic or current card. Codex sessions have no Claude terminal controls.

## Reconnect and delivery limits

After a disconnect the listener resumes registered threads without starting a
turn. It obtains the active turn ID before allowing steering. When a turn it
observed as active finished while disconnected, it checks recent persisted
turns and tries to recover that turn's final reply. The per-thread reply ledger
stores the newest 512 turn/item identities before sending. This prevents a
retry after an ambiguous Telegram result, at the cost of losing a reply if
the listener stops after recording the identity and before Telegram accepts it.
Cockpit shows when a reply delivery could not be confirmed. A turn wholly
missed while offline is outside this recovery window. If a pending request is
replayed by Codex, it gets fresh cards.

Synthetic coverage uses a fake app-server over a socketpair and a fake
Telegram bot. Live app-server, Telegram and TUI interoperability remain
unverified. Structured command/network policy amendments, positive MCP
elicitation responses, secret input, media and Claude-specific controls
stay outside the Telegram app-server bridge.

## Persisted observer

`ctb.harness_codex.CodexHarness` implements the shared Harness boundary for
existing local sessions. It does not launch a model or attach a second writer.
The adapter is off by default. Construct the registry with the loaded config
(`registry(cfg=cfg)`) to enable it:

```json
{
  "codex_harness_enabled": true,
  "codex_attached_sessions": ["00000000-0000-4000-8000-000000000001"],
  "codex_commentary": false
}
```

Replace the sample UUID with the complete native ID of an existing persisted
session. Discovery alone does not attach it. `mirror: false` suppresses all
assistant text events. Commentary requires its own opt-in. `CODEX_HOME` selects
the CLI home; `codex_sessions_dir` may override the observation directory, but
queueing is unavailable if that directory does not match the CLI home. Direct
adapter construction can bind a different home/profile explicitly.

When Harnessgram starts the app-server, it uses a minimal environment. Alongside basic process variables,
it passes configured proxy and TLS certificate variables plus existing `CODEX_*` and `OPENAI_*` variables.
Unset variables are not added.

## Observed format and canonical output

The locally installed CLI identifies itself as `codex-cli 0.163.0`. Read-only
structural inspection established newline-delimited JSON under
`sessions/YYYY/MM/DD/rollout-*.jsonl`:

- Records have `timestamp`, `type`, `payload`, and sometimes `ordinal`.
- `session_meta` identifies the native `id`/`session_id` and `cwd`. Metadata
  includes private instructions/account fields; these are never copied out.
- `event_msg.task_started` supplies the turn ID. `turn_context` supplies model
  and effort. `event_msg.token_count.info.last_token_usage` supplies input,
  output and total token counts when available. Total tokens are an observed
  context estimate, not a context-window size or account quota.
- `response_item.message` with role `assistant` and phase `final_answer` or
  `commentary` contains `output_text` blocks. Native message IDs and
  `internal_chat_message_metadata_passthrough.turn_id` are present in sampled
  records. Unknown phases are excluded. Tool calls/results use `call_id`.
- `event_msg.task_complete.last_agent_message` can repeat the final answer.
  Only response items produce reply events. Reasoning, tool inputs/outputs,
  user/developer/system messages and child activity are not reply sources.

Synthetic tests reproduce these field shapes, never real session content.
Unknown records are ignored. Persisted existence does not prove a live process.
Compressed archives, ephemeral sessions and exec JSON streams are not covered.

## Cursors and delivery

First attachment starts at the latest whole record, without forwarding historical
answers. Persist `initial_cursor(ref)` even when there are no new events; pass it
to `events(ref, cursor)` on subsequent reads. Use `initial_cursor(ref, replay=True)`
or `events(ref, replay=True)` only when historical replay is explicitly requested.

Persist an event's opaque cursor after handling it. Version 2 binds the full
harness/profile/session identity and retains only byte position, file identity,
a boundary hash, the last turn ID and the last emitted event's identity hash.
Version 1 cursors are rejected; reattach at the current end when upgrading.
The recent deduplication ledger (128 events), model and tool context remain in
bounded memory. Cold restarts rebuild context up to the saved boundary. Replaced
files are scanned for that boundary before emitting subsequent events; if the
boundary is missing but its turn remains, that ambiguous turn is suppressed.
If rotation removes the saved turn entirely, the replacement is read from its
beginning; deduplication does not cover arbitrary historical rewrites.
Incomplete lines (including partial UTF-8) are retried. Metadata reads are capped
at 64 KiB and event records at 1 MiB; oversized records are skipped as whole lines.
Only regular files opened without following symlinks are accepted.
A crash between outward delivery and cursor storage still requires consumer-side
idempotency. No adapter can make those two writes atomic. Ignored tails may be
rescanned until the next emitted event.

Native `codex queue --help` documents `--thread <THREAD> --message <TEXT>`.
The adapter probes this surface without injecting anything, then queues only
an explicitly attached, discoverable full UUID, preserving existing Telegram
origin framing. Ordinary text never approves native requests. A successful
process result means `ReceiptState.ACCEPTED`, never observed ingestion or an
answer. Failure/timeout is visible and never automatically retried; diagnostic
stdout/stderr is not exposed. End-of-turn holds are unsupported.
The verified queue surface has no stdin/file input, so framed text uses argv with
a random fence independent of the receipt key. Framed text over 64 KiB, embedded
NULs and OS argument-size errors are definite rejections, never unknown acceptance.

The persisted observer provides text queueing. Native start, resume, steering,
interrupt and approvals are available for registered app-server threads. The observer
does not drain any inbox or send Telegram messages itself.

## Persisted observer topics

The existing heartbeat calls `ctb.codex_mirror.poll` once per round (normally
every 30 seconds), only with `codex_harness_enabled: true`. With the flag off,
the heartbeat makes no Codex calls and creates no Codex state or topics. The
global mirroring switch still gates the heartbeat. No discovery result is
implicitly attached: only the full IDs in `codex_attached_sessions` are polled.

Each attached, readable session gets a topic through the existing `TopicMap`,
named with its usual naming rules and a short `Codex` marker. The existing
`topics.json` map remembers its namespaced `codex:default:<native-id>` key,
`thread`, `name` and `visible_name`. Replies never fall back to General when
a Codex topic cannot be created. Explicit topic renames keep their precedence.

`codex-mirror/<native-id>.json` stores one new key, `cursor`, containing the
adapter's opaque cursor. The first poll saves `initial_cursor(ref)` even if
there are no new events, so old replies are not replayed. A per-session lock
covers consumption and delivery; the existing private atomic writer saves the
cursor after **each handled event**, including tool/usage events. At most 50
events per session are handled in one round; the rest wait for the next round.
A failed session is logged without content or exception diagnostics and does
not stop the others. Failed reply delivery leaves its cursor unadvanced.
A crash after outward delivery but before the cursor write can still repeat a
message; Telegram delivery and local storage do not provide one transaction.

Final assistant text uses the existing `Mirror.send_reply` path and outgoing
sanitizer, labelled `Codex`. Commentary follows the same path only when
`codex_commentary: true` (off by default). Turn start/end update the existing
working/done topic indicator; they do not post lifecycle announcements.
Tool progress and usage produce no chat messages. Claude terminal probing and
placeholder refresh are skipped for these topics. This slice does not add a
Codex Cockpit/radio projection or claim native account/liveness information.

Fresh owner messages in an attached Codex topic use the shared topic-input
preparation and `_queue_owner`, then `CodexHarness.send`. Existing owner checks,
secret rejection, voice-review requirements, quotes and forwarded-content
framing remain in force. Native queueing does not apply Claude end-of-turn
holds or launch/nudge a Claude process. Failures get a short notice with no
native diagnostics and no automatic retry. Queue acceptance still does not
prove ingestion or an answer. Native approvals, panel/keyboard controls and
edits remain refused; a non-owner receives no refusal reply or callback answer.
Voice/media control parity remains outside this text-only slice.

Synthetic coverage lives in `tests/test_codex_mirror.py`. No live Telegram
delivery or native ingestion check is established by these tests.
