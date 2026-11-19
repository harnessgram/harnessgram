# Local MCP connector

Harnessgram's MCP server lets an MCP client inspect local sessions and send clearly labelled messages to them. It is
local-only: stdio is the default, and HTTP always binds to `127.0.0.1`.

Install the optional SDK and start it with:

```sh
uv tool install 'harnessgram[mcp]'
ctb mcp serve --stdio
```

For Claude Code, add the stdio server locally:

```sh
claude mcp add --transport stdio harnessgram -- ctb mcp serve --stdio
```

## Tools

* `list_sessions` lists names, projects, working/idle/waiting state, last activity, and topic titles as untrusted data.
  It lists only the calling session (stdio) or the authenticated session (loopback HTTP).
* `read_replies` reads at most 20 recent agent replies. In both stdio and loopback HTTP modes it accepts only the
  calling Claude Code session.
* `send_to_session` queues a topic-style message, labelled as coming from MCP and explicitly not an approval. Like
  `read_replies`, it accepts only the calling session.
* `status` reports per-account quota, radio state, the caller's open request ledger count, and listener health.
* `ledger_list` and `cards_list` are read-only and return untrusted data: only the calling session's cards, and
  only ledger items of the calling session's project or items that name the calling session.
* `answer_routine_ask` answers only asks the bridge has already classified as routine, and only the calling
  session's asks. Sensitive asks are refused.

Every tool first resolves its caller and fails closed: with no proven stdio session, no authenticated HTTP session,
or tools bound to no caller at all, every call is refused before any bridge state is read, and the server refuses to
start with unbound tools. Sessions, cards, and ledger rows are matched on the full session id, so two sessions that
share a short id never see each other's rows. A test enumerates the registered tools and calls each one through the
real HTTP and stdio servers, so a new tool that shows another session's rows fails the suite.

There are deliberately no delete, install, merge, money, account, publication, promotion, or store-submission tools.
All session, reply, card, and request ledger results are returned inside an `untrusted_data` wrapper. MCP messages
use their own `<mcp-message>` fence. Suggestions are discarded before owner messages when a queue is full, while
MCP answers to closed routine asks are retained like control-panel answers. Neither counts as owner approval—even
if its text is `yes`. Routine answers are recorded with `answered_via: "mcp"`.

## Loopback HTTP and authentication

Create or rotate the bearer token explicitly. The value stays in the macOS Keychain: Harnessgram does not put it
in a command, file, environment variable, shell history, or log. The printed setup command registers
`ctb mcp header` as Claude Code's `headersHelper`; that helper reads the existing Keychain item and emits the JSON
authorization header only when Claude Code starts the connection. The helper identifies the calling session and
derives a session-bound HMAC bearer credential from the Keychain secret; it also sends the session identifier in
`X-Harnessgram-Session`. The master secret is never emitted.

```sh
ctb mcp token --rotate
# Run the secret-free `claude mcp add-json ...` command it prints.
ctb mcp serve --http --port 8765
```

HTTP clients must send both session-bound headers (Claude Code obtains them from the helper). Missing headers,
incorrect signatures, and signatures paired with a different session identifier receive HTTP 401. Every request
is checked on its own: the middleware records the verified session in that request's ASGI scope, and each tool reads
it from the MCP request context of the message it is handling, so a later request on the same MCP connection cannot
reuse an earlier request's session. A tool call with no verified session is refused. Each request can list, read,
send to, or answer routine asks only for its authenticated session. The server does not accept a public bind
address; a public relay is intentionally outside this stage.
