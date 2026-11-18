> **Owner decision (17.11): API-key sessions only.** Headless sessions are available only when the user configured
> their own API-key authentication. HarnessGram never reads, stores, or logs that key: the unmodified Claude CLI
> receives the environment and settings exactly as configured. Subscription (`claude.ai` login) sessions remain in
> tmux. `headless_enabled` defaults off and every session's `runner_mode` defaults to `tmux`.

# HarnessGram headless bridge: design (2026-11-17)

Goal: stop reading and typing into the tmux screen. Each bridged Claude Code session runs as a
headless `claude -p --input-format stream-json --output-format stream-json` process owned by a
per-session runner. Read-only design, checked against Claude Code CLI 2.1.322.
Models used in the spike: Haiku; total spend about $0.03 of subscription quota.

## 1. Verified facts

Four CLI runs (three stream-json drivers plus one TUI resume in an isolated tmux socket `-L hgspike`).
Runs used `--setting-sources project --strict-mcp-config` so the owner's real bridge hooks never fired.
Deviation from the brief: `--max-turns 3` instead of 1, because a tool call plus its answer needs 2+ turns.
Side effect: Claude Code also wrote its own session transcripts in its normal projects folder.

| Question | Answer | Evidence |
|---|---|---|
| One long-lived process takes several user messages over stdin? | **Yes.** 7 turns in one process and one `session_id`; each turn ends with its own `result`. | run1, run2 |
| A message written while Claude is busy? | **Queued.** It ran as the next turn after the busy one ended (Q1 then Q2). Docs: a message sent during tool calls is folded in after those calls finish. | run2 |
| `"/compact"` as a user message compacts? | **Yes.** `system/status status=compacting` -> `status=null, compact_result=success` -> `system/compact_boundary` (`trigger: manual`, `pre_tokens 18821 -> post_tokens 3678`, `duration_ms`) -> `result`. Context survived (recalled "Blue" and "ALPHA"). | run1 |
| Interrupt (Esc)? | **Yes.** `control_request {subtype: interrupt}` -> `control_response {still_queued: []}`, and the turn ends with `result subtype=error_during_execution`. The process stays usable. | run2 |
| Model switch? | **Yes.** `control_request {subtype: set_model, model: haiku}` -> response `{model: claude-haiku-5-5}`. `set_permission_mode` -> `{mode: plan}` works the same way. | run1, run2 |
| `--resume` headless <-> TUI? | **Yes, both ways, same id.** The TUI `claude --resume <id>` showed the whole headless history (and restored plan mode). A message typed in the TUI ("GAMMA") was seen by a later `claude -p --resume <id>` that quoted "QUEUED, GAMMA". | run3, run4 |
| PermissionRequest hook in `-p`? | **Yes, it fires** (stdin has `session_id`, `transcript_path`, `tool_name`, `tool_input`, `permission_mode`) and its `allow` decision is applied. | run2 `hook.log` |
| AskUserQuestion / ExitPlanMode in `-p` without a permission host? | **They are removed from the tool list.** Claude answered "AskUserQuestion isn't among my available tools", and the same for ExitPlanMode. A hook alone is NOT enough. | run2 |
| ...with a permission host (`--permission-prompt-tool stdio`)? | **They work.** The CLI sends `control_request {subtype: can_use_tool, tool_name, input, permission_suggestions, ...}`. Reply `control_response {behavior: allow, updatedInput: {questions, answers: {<question>: <label>}}}` answers the question; the same works for Bash, Write and ExitPlanMode (input has `plan`). | run1, run4 |
| Hook and host both set? | **Both are consulted for the same request** (the hook logged Bash/AskUserQuestion/ExitPlanMode; the host got all of them plus Write). **Which answer wins is unverified**; treat it as a race, as in the TUI. | run4 |
| Trust and "resume this conversation?" dialogs? | None in `-p` (docs: "A `-p` session shows no workspace trust dialog"). The TUI showed both the trust and the external-imports dialogs on the same folder. | run3, docs |
| Auth in the spike | `system/init.apiKeySource = "none"` = the subscription OAuth login. `--bare` would refuse OAuth (docs). | run1 |

Stream event shapes (stdout, one JSON per line; with `--verbose`):
- `system/init` once per turn: `session_id, cwd, model, permissionMode, tools[], mcp_servers[], slash_commands[], terminal_slash_commands[], plugins[], capabilities[]`.
- `assistant`: content blocks, usage and `parent_tool_use_id`; `user`: tool results and permission decision metadata.
- `result`: subtype, result text, turn count, usage, cost, denials and session id.
- `rate_limit_event`, `system/status`, `system/compact_boundary`, `system/api_retry`, and `system/permission_denied`.
- Host -> CLI: `user`, `control_request`, and `control_response` messages.

Unverified: hook-versus-host winner; `/effort x`, `/model x` typed as text; images in user content; hooks receiving
the runner's environment variables; resume across two `CLAUDE_CONFIG_DIR` account profiles; long-run memory of one
process over days. The raw stdio control protocol is what the official SDKs speak, but the docs document it through
the SDK APIs, not as a CLI contract.

## 2. Licensing and terms (quoted, not interpreted)

Anthropic's Agent SDK and legal documentation say third-party products should use API-key authentication and may not
offer or intermediate claude.ai login without approval. The user's credentials must remain their own, and HarnessGram
must never collect, store or move credentials or session tokens. The safe public shape selected by the owner is
therefore API-key-only headless operation; subscription sessions continue to use the unmodified TUI in tmux.

## 3. Architecture

### 3.1 Session runner (one per session)
- New module `ctb/runner.py`, CLI `ctb runner <session>`: one `claude -p --input-format stream-json
  --output-format stream-json --verbose --permission-prompt-tool stdio --replay-user-messages [--resume <id>]
  [--model m] [--permission-mode m]` child. Normal settings sources let existing user hooks run.
- Exclusive `fcntl` lock `<state>/runners/<digest>.lock` prevents two writers. The socket, lock, state directory,
  child record, and event log all use the same SHA-256-derived session digest, so names which sanitize alike cannot
  alias each other.
- A 0600 Unix socket `<private>/runners/<digest>.sock` accepts `send`, `interrupt`, `set_model`, `set_mode`, `answer`
  and `stop`; an append-only event file contains metadata, never message text, tokens, tool input, or key material.
- Every connection's peer uid comes from the kernel: `getsockopt(SOL_LOCAL, LOCAL_PEERCRED)` (`struct xucred`,
  version checked) on macOS and `SO_PEERCRED` on Linux. A mismatched or unverifiable peer, an I/O error, or a client
  that does not finish its request line within 5 seconds closes that one connection and logs only a reason code; the
  runner loop never ends because of a client.
- Child stdout is read from the raw, non-blocking file descriptor into the runner's own byte buffer, which is split
  into complete lines. A buffered `readline` is never used under the selector, because it can hide further lines from
  it. When the child exits, the pipe is drained and a final unterminated line is consumed too.
- The CLI's own session transcript is delivery truth: the uuid of a stdin user message is recorded as that user
  record's own `uuid` (verified with one real `claude -p --input-format stream-json --replay-user-messages` turn on
  Claude Code 2.1.322: the stdin `uuid` was echoed on stdout and stored unchanged as the transcript user record's `uuid`). Pending storage is only `uuid -> wire message`, written atomically. Before a
  pending item is sent at startup, and whenever its UUID is submitted again, the runner scans the transcript selected
  by the stream session id and working directory. A UUID already recorded there is removed and never written again;
  one absent there is resent after restart. There is no acknowledgement ledger.
- The transcript lives in `<CLAUDE_CONFIG_DIR or ~/.claude>/projects/<folder>/<session_id>.jsonl`, where `<folder>`
  is the real working directory with every character other than `[A-Za-z0-9]` replaced by `-` (so `/.` becomes
  `--`; the rule applies per UTF-16 code unit). Claude Code truncates long names to 200 characters plus a hash suffix;
  when the computed folder does not exist, the projects root is searched one level deep for `<session_id>.jsonl`.
- Broken transcript lines (a torn final write, invalid bytes) are skipped and counted in a metadata event; they never
  disable reconciliation of the other records. If the transcript itself is missing or unreadable, the runner does not
  resend and emits only a metadata warning, preferring at-most-once behavior when truth cannot be established. A
  message that was pending before any `system/init` (no session id yet) cannot be in any resumable transcript and is
  resent to the next child.
- A failed write to the child's stdin means the child has exited: the message stays pending (the client is told
  `state: pending`) and the normal restart path delivers it.
- Stall detection: silence never counts as a stall while a CLI permission or control request is open (until it is
  answered or cancelled) or while a `tool_use` has no matching `tool_result`. Otherwise a turn that stays silent for
  15 minutes is stopped and restarted; every event resets the timer.
- Child exits restart with `--resume <session_id>` and capped 2, 10, 60 second backoff. Interrupt uses SIGINT before
  termination. Before respawning, the old process group receives the same escalating stop sequence and its absence
  is confirmed, covering surviving grandchildren. On startup a recorded group is stopped only when its leader's
  identity still matches: Linux uses `/proc/<pid>/stat` start time and macOS uses the microsecond `p_starttime` from
  `sysctl kern.proc.pid`; an unreadable or mismatched identity is never signalled. `child.json` is written
  immediately after `Popen` returns. The remaining millisecond-scale interval between process creation and that
  atomic record is an accepted risk.
- Linux also sets `PDEATHSIG` and rechecks `getppid` after `prctl` to close the parent-death race. It protects the
  direct child only; the process-group escalation, rather than `PDEATHSIG`, is what covers grandchildren. Pending
  permission requests are never auto-allowed.
- Event types and subtypes are allowlisted. Session ids, request ids, and UUIDs appear only as short keyed digests
  or presence flags, never as raw identifiers.
- Never pass `--max-turns`; pass `--max-budget-usd` only for API-key sessions.

### 3.2 Feature mapping
Owner delivery becomes runner `send`; busy messages queue in the CLI. Interrupt, model, permission-mode and compaction
use stream control messages. Existing hooks continue mirroring and progress in phase 1. The permission host owns
AskUserQuestion and ExitPlanMode. Quota and compaction use their structured events rather than screen scraping.

### 3.3 "Open on the Mac"
The runner hands off only after the current result (or an explicit interrupt), retains a handed-off lock, and opens
plain `claude --resume <id>`. When the TUI exits, the runner resumes. This is a later PR.

### 3.4 Code that can be deleted after full migration
After tmux is removed, screen parsing and typing in `dialogs.py`, `inbox.py`, `session_reload.py`, `autopilot.py`, and
`compact.py` can be retired with their screen-specific tests. Estimated net removal: about 5,000 lines plus tests.

## 4. Rollout
- Global `headless_enabled` defaults off; per-session `runner_mode` defaults `tmux`.
- Canary one low-stakes session for three days, then three sessions, then new sessions; retain tmux for two weeks.
- Measure lost messages, permission round-trip latency, stuck turns, restarts, compaction and interrupt success.
- Roll back by stopping the runner and opening the same id with `claude --resume` in tmux.
- Every PR is draft, tested locally, and reviewed independently; new UI text uses Hebrew-default i18n.

Ordered PRs: runner core; permission host; inbox delivery; session launch; Mac handoff; quota/autopilot; canary metrics;
then small deletion PRs after two quiet weeks.

Testing rule: every mechanism is tested through the real operating-system path: real Unix sockets and socket pairs
for peer credentials and client handling, real pipes driven by a fake CLI script (including several lines in one
write), and real temporary directories for transcript lookup. Tests never mock the code under test (peer-uid
reading, the stdout read path, or transcript folder naming), never run an installed Claude CLI, and always point
`CLAUDE_CONFIG_DIR` at a temporary folder.

## 5. Risks
- The stdio protocol is undocumented; pin a minimum CLI, feature-detect capabilities, and contract-test it.
- Hook and host permission races require silencing the hook in runner sessions.
- Two writers lose messages, so lock and handoff are mandatory.
- Headless trust behavior requires refusing folders not previously trusted.
- Hooks that type into panes must check runner mode.
- Long-lived processes and resume across account profiles still need canary verification.
- Accepted: a runner that dies between the child's `system/init` and saving the session id restarts without it, so
  pending messages go to a new session and may run twice (a window of milliseconds).
- A tool that hangs forever is never treated as a stall, as in the terminal app.
- The Linux `SO_PEERCRED` path has not run yet; run the tests on Linux once before any Linux deployment.

BLOCKER B1 is resolved by the owner decision: public headless operation is API-key-only. B2 (hook/host winner and
environment), B3 (cross-profile resume), and B4 (permission host required for question and plan tools) remain for the
later PRs that own those features.
