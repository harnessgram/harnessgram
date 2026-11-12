# Shared harness boundary

`ctb.harness` defines the shared contract; it does not enable another provider.
The first stage delegates Claude discovery and queue writes to the existing
readers and `Inbox`. Telegram authorization, credential checks, message framing,
delivery policy, hook observation, wake behavior and terminal controls retain
their existing owners.

## Identity and legacy lookup

`SessionRef(harness, profile, native_id)` is the identity. Its `key` is a
canonical, escaped `harness:profile:native_id` string for state maps. Resolve
the whole key; never select a session by a native ID prefix. The registry is
keyed by the exact harness/profile pair and refuses unknown adapters.

A string outside the explicit `harness:profile:native_id` form resolves to
`claude:default:<id>`. Legacy IDs retain their exact spelling, including mirror
titles with spaces, Unicode or colons outside that form. Transcript readers keep
their path traversal checks and escape glob characters when searching these IDs.
`default` denotes the existing Claude account-routing policy, including account
moves; it is not an account name. `HarnessRegistry.lookup` reads a namespaced
key first, then falls back to the raw key only for Claude's default profile.
This permits gradual migration without rewriting shared state files. Other
harnesses and profiles cannot inherit a raw Claude entry.

Only the default Claude adapter is registered. It writes to the existing raw-ID
queue, so installed hooks can continue consuming messages. The owner-message
route normalizes identity before legacy bookkeeping or wake behavior. Claude
transcript lookup accepts legacy IDs and explicit default-Claude identities,
and refuses another harness/profile even when its native ID matches.

Namespaced identities are supported by the adapter API and lookup layer only.
Persisted Telegram topics keep raw Claude keys in this stage. Inbound topic
messages, edits and buttons reject namespaced keys before legacy dispatch,
including Stop and dialog handling. Refused messages receive a short visible
reply and refused button taps are answered. Owner queue routing also logs and
returns on an unsupported identity, so a saved dispatcher or voice target cannot
block later updates. Enabling namespaced topic routing requires
migrating all policy/control lookups together in the next stage; normalizing
only the final queue write would leave earlier controls using the wrong ID.

## Capability and delivery limits

Capabilities are versioned and false unless explicitly supported. Discovery
returns known topic snapshots through existing transcript/model readers. It
neither enables forwarding nor infers process liveness from a transcript.

The typed event contract includes turn start/end, assistant commentary/final
text, tool progress, questions/approvals and usage. Native turn, item, request,
timestamp and cursor fields are retained when known; unknown usage stays
unknown. Claude's existing hooks remain the active observation pipeline. A
cursor-based pull stream is not advertised or synthesized from transcript
heuristics in this stage.

`send` preserves the complete legacy message mapping, including owner/team,
forwarded, voice, button, typed-input and hold/framing fields. A successful
local queue write returns `queued`; exceptions remain errors. `accepted`
means native transport acceptance, `observed` means ingestion was observed,
and `answered` means a correlated response was observed. Only the latter two
prove delivery. No adapter promotes a receipt merely because a command exited
successfully.
Each receipt carries the queue's per-message key for later evidence correlation.

Shared controls are typed but unsupported in the initial Claude wrapper.
They raise `UnsupportedOperation`, including approval and answer requests;
they cannot queue an approval or fall back to terminal keystrokes. Existing
Claude controls remain unchanged outside the shared API.

## Next stage: Codex MVP

- Register a separate, opt-in Codex adapter; keep discovery separate from
  forwarding consent. Use exact thread/profile identity throughout routing,
  topic state and persisted observation cursors.
- Validate the installed native queue contract. Preserve errors and distinguish
  acceptance from observed ingestion; never start a concurrent resume writer
  merely to inject into a live thread.
- Parse persisted rollouts separately from owned `exec --json` output. Mirror
  only selected assistant commentary and final text, deduplicating native
  turn/item identities and excluding instructions and reasoning.
- Add sanitized fixture coverage for restart/replay, partial UTF-8 records,
  file replacement, duplicate/late completion, failed queue writes and
  accepted-but-unobserved messages. Preserve mirror-off and egress filtering.
- Feed normalized model/usage and coarse session state to Cockpit/radio. Verify
  one opted-in canary's inbound ingestion, reply and displays independently.
  Keep native approvals, terminal menus and richer control unsupported until
  request-bound protocol and security tests establish their behavior.
