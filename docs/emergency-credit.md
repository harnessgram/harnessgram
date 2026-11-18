# Emergency credit continuation

Paid continuation uses `ctb credit-key` as its credential gate. The helper
requires an active approval for the exact `CTB_CREDIT_SESSION`, checks today's
shared transcript estimate and the optional provider meter, and refuses with
empty stdout and a nonzero exit when the higher spend is unknown or at least
$8.50 ($10 minus a $1.50 safety margin). It captures `anthropic-key-helper`
stdout in a pipe with a ten-second timeout, then rechecks approval, the billing
window, subscription recovery and measured spend before releasing any bytes.
The key is held only in memory and is never logged, stored, or passed in arguments
or environment variables. Failed or timed-out helpers release no bytes.
The command is local-only and does not load the bridge configuration.

`CLAUDE_CODE_API_KEY_HELPER_TTL_MS=60000` requests credential renewal every minute.
Clients may retain a cached key after failed renewal, so the heartbeat also
terminates blocked paid processes: TERM first, then KILL after a shared
three-second grace. Each signal rechecks the exact PID/start identity, Claude
executable, credit binding and session records; other sessions and replacement
processes are never targets. Unreadable process identity prevents signalling
and leaves the credit binding blocked for a later retry.
This is not an atomic reservation of request costs: already-running requests
and concurrent sessions can still add cost before the heartbeat stops them.
The margin reduces that risk;
it does not guarantee a $10 billing ceiling. No strict provider-side
billing ceiling or invoice accuracy is claimed.

`emergency_credit` defaults to true and is exposed in both Cockpit settings
layouts. It offers a decision only after the existing account fallback finds no
subscription account with room. One private, non-routine decision is shared by
the session topic and Cockpit. Owner/topic and original-message checks protect
Telegram callbacks, and forwarded messages are refused;
the normal authenticated decision endpoint protects Cockpit answers. Neither
answer is delivered as an instruction to Claude.

`credit` is a reserved session-only pseudo-account, never a profile or the
default for new sessions. Approval is single-use for that exhaustion. The
heartbeat consumes the persisted answer and calls the existing serialized
account move, replay, and resume helpers. The resume command keeps the session
id, current model and permission mode and supplies only `apiKeyHelper` and
`forceLoginMethod: console` through shell-quoted `--settings`. Inherited auth
and endpoint overrides are removed. Credit launches use `--setting-sources user`
to exclude project and local settings. As defence in depth, preflight still
refuses authentication overrides in user, account, project, local and managed
settings. It also checks the bridge's inherited `CLAUDE_CONFIG_DIR/settings.json`
and the explicit `CLAUDE_CODE_MANAGED_SETTINGS_PATH` and
`CLAUDE_CODE_REMOTE_SETTINGS_PATH` files found in the installed 2.1.322 client.
Explicit files must be readable, valid settings objects even though their path
variables are removed from both the launch environment and pinned settings.
Refusal logs never include file paths, settings values or parser details.

The private `emergency-credit.json` ledger records approval, session, origin,
local day and billing boundaries. Billing starts after the old process exits,
before the credit resume command; it ends before subscription resume. Estimates
include new subagent transcripts, deduplicate message usage and share the
existing `session_usage.PRICES` table. Prices remain estimates, not a verified
provider invoice. The daily total is shared by all activations and cannot
decrease within a local day.

When installed, `anthropic-credit` is checked at offer and active heartbeat time
with a ten-second timeout, including each credential renewal. Accepted output
is explicit daily USD spend or remaining USD balance, as JSON
(`spent_today_usd`, `daily_spend_usd`, `today_spend_usd`,
`spent_today`; `balance_usd`, `remaining_usd`, `balance`) or labelled text. The
greater of transcript spend, reported daily spend and observed balance decrease
counts. Raw tool output is never stored or logged. An installed but unreadable
meter, unknown prices, unreadable transcripts or subagent directory scans, and
invalid saved dollar amounts block new paid turns.

The heartbeat revokes every active approval and interrupts all paid turns when
measured spend reaches $8.50, or when spend becomes unknown. It terminates all
blocked or revoked paid processes before any slow subscription move, including
when a pane is missing or reused. No process is stopped for a deferred approval
whose billing interval has not started. The existing move-back/recovery path
parks each session on its subscription. Queued messages
are preserved. The Stop hook preserves queued messages;
UserPromptSubmit and async Stop wakeups block new paid turns. Credential renewal
adds the spend check at the API authentication boundary.
The status card and Cockpit show the shared estimated daily cost. At the reported quota
reset, or a newer usage reading with room, the existing safe-idle checks gate
the move back to the subscription. When process records prove there is no live
process, recovery clears the credit binding directly without launching anything;
the next start uses the subscription. Unknown process state cannot clear the
binding. A later exhaustion needs another tap.

Approval is checked against the current account, verified transcript API limit
and available subscription alternatives both at the tap and before activation,
including deferred moves. An expired authorization cannot be relaunched after
a crash. An unclosed billing interval remains available only for accounting and
subscription recovery. Ordinary assistant prose cannot mark an account limited;
the mirror requires a verified transcript error or the StopFailure error hook.

Paid processes carry a non-secret originating session marker. A conversation
switch (`/clear` or `/resume`) blocks new paid prompts immediately; SessionStart
closes the previous transcript interval and records a revoked interval for the
new transcript, retaining usage and returning the actual conversation to its
original subscription when ready. Consent never follows the switch.

Focused verification uses fake panes, meters, transcripts, and isolated state.
For a Git-free hygiene scan, use a clean source snapshot without the linked
worktree's `.git` pointer file. A test-runner subprocess guard can refuse Git so
the unchanged hygiene test uses its existing folder-walk fallback; the pointer
would otherwise be mistaken for public source containing a personal path.
No live authentication, provider output, installation or billing was exercised.
