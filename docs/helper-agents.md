# Helper agents

Small, cheap Haiku jobs that look after the bridge. Each has its own switch and **all are off by default**. With a
switch off the job reads nothing, runs nothing and spends nothing.

```json
"helper_agents": {
  "card_titles": false,
  "patrol": false,
  "ledger_capture": false,
  "route": "subscription",
  "api_fallback": false,
  "daily_usd_cap": 0.5,
  "api_until": ""
}
```

Every key is optional. An unknown key, a wrong type, a cap outside 0 to 100 or a malformed date makes the whole
section invalid, which reads as everything off; the bridge refuses to start with it and names the key.

## Jobs

- **`card_titles`**: Hebrew title and step names for the bridge's own live progress cards. One call per change of
  the task list, cached; every label is checked (Hebrew, short, at most two Latin product names). The offline NLLB
  title stays the fallback, and a failed call is not repeated for 10 minutes.
- **`patrol`**: every 10 minutes a scan in plain code (no model) collects stale progress cards, sessions that say
  they are working but have been silent for 30 minutes, and failed reply deliveries. Only when it finds something does
  the model write one short Hebrew note with a suggested fix, shown as a private Cockpit card (at most three per scan,
  the same finding at most every 6 hours). The patrol can do two things, chosen by code and never by the model: close
  a progress card that has been stale for hours, and put failed reply parts back in the delivery queue. It never sends
  to other people, deletes data or touches accounts, money, publication or permissions.
- **`ledger_capture`**: when you type in a topic, the model decides whether the message is a request, an idea or a
  question worth tracking. If so, a Cockpit card proposes one ledger item with **Add** and **Dismiss**. Nothing is written
  until you tap Add (through the same ledger write the knowledge overlay uses, project `knowledge.ledger_project`). It
  never closes or edits an existing item. Messages that look like secrets are never sent to the model.

## Routes and spend

- `route: "subscription"` (default): a headless `claude -p --model haiku` on your own subscription, with no tools,
  servers, hooks or saved session, the prompt on stdin and a 45 second timeout. A failure falls back to the job's
  non-model behavior, or to the API when it is enabled.
- The API is used only with `api_fallback: true` (and always when `route` is `"api"`, which skips the subscription).
  The key comes from the existing key helper, is held in memory for one request, and is never logged, written to the
  config or echoed.
- `daily_usd_cap` (default 0.5) is a hard daily limit. Every call is recorded in a private usage file; a call that could
  cross the cap is not made.
- `api_until` is an ISO date. After it, and while it is empty, the API route is refused until you change it, so it is
  never pay-as-you-go.
- The API route stops for the rest of the day, with no retries, when the cap is reached, the key is missing or refused,
  or the API answers that credit is exhausted or billing failed.
- The subscription route is limited to 400 calls a day.
