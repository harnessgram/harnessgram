# App Review demo bridge

The demo bridge is a deliberately separate service for the Harnessgram iOS review experience. It exposes only the
Cockpit snapshot, decisions, widget snapshot, radio-player metadata, Live Activity acknowledgement, and pairing APIs.
Its realistic sessions, quotas, decisions, progress card, and radio status are compiled into the package. A harmless
decision updates process memory; the complete fixture is restored on the next UTC hour boundary.

It does **not** read Harnessgram configuration, Telegram credentials, system credential storage, or agent state. It
does not start an agent or invoke desktop automation. Unknown and sensitive actions return HTTP 403. The process has
no persistence volume and must be deployed separately from a personal bridge.

## Run locally

Choose a random reviewer code of at least 12 characters and a random signing secret of at least 32 bytes. The signing secret must be unique to this demo
deployment and must never be copied from a real bridge.

```sh
export CTB_DEMO_CODE='REVIEW-CODE-FROM-APP-REVIEW-NOTES'
export CTB_DEMO_SIGNING_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
ctb demo serve --host 127.0.0.1 --port 8080
```

`POST /api/demo-pair` (also accepted at `/api/pair` and `/api/device-pair`) exchanges `{"code":"…"}` for a
demo-only bearer token. The token is a stateless, signed credential valid for the configured window (30 days by
default), so the same code works on several review devices and continues to work after a restart or across identical
instances. Send it as `Authorization: Bearer …` to all other endpoints. Pairing attempts are rate limited per client
and across the service. By default, the direct peer address identifies the client. Set the comma-separated
`CTB_DEMO_TRUSTED_PROXIES` list to the ingress proxy addresses that may supply `X-Forwarded-For`; headers from every
other peer are ignored.
Set `CTB_DEMO_TOKEN_WINDOW_SECONDS` to a positive integer to override the default token window.

## Container deployment

```sh
docker build -t harnessgram-demo .
docker run --rm -p 8080:8080 \
  -e CTB_DEMO_CODE="$CTB_DEMO_CODE" \
  -e CTB_DEMO_SIGNING_SECRET="$CTB_DEMO_SIGNING_SECRET" harnessgram-demo
```

Terminate TLS at the small host's managed HTTPS ingress. Do not mount volumes, forward personal bridge traffic, or
provide Telegram, APNs, cloud, or agent credentials. Use `/healthz` for the unauthenticated health probe. Redeploy or
keep the signing secret consistent across demo instances during the review window.

The 64-connection process bound protects the service as a whole, but slow clients can occupy all of those slots.
Enforce per-IP connection and request limits at the managed ingress, before traffic reaches the demo process.

## API behavior

* `/api/state` provides three session states, two quota meters, decisions, a progress card, and radio metadata.
* `/api/decide` changes only a non-sensitive demo decision in memory.
* `/api/widget-state` and `/api/widget-decide` provide the corresponding limited app/widget flow.
* `/api/radio-player` supplies display metadata but no external audio URL.
* `/api/live-activity-token` acknowledges the UI exercise without retaining or forwarding a push token.
* Every API outside this allow-list, and the included sensitive decision, is refused.
