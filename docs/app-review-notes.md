# Draft App Review notes

Harnessgram is a companion for people supervising coding agents. The iPhone app is a remote Cockpit: it summarizes
active and idle work, presents explicit decisions, shows usage allowance, provides a team-radio view, and offers
widgets and Live Activity progress. The review demo is synthetic and cannot access any person's computer or account.

## Review access

1. Open Harnessgram and choose **App Review Demo** on the pairing screen.
2. Enter the review code supplied in the App Store Connect review notes: **`<INSERT CURRENT DEMO CODE>`**.
3. The code pairs only with the isolated demo service. It grants no access to a customer account or production data.

The code can pair several review devices. Pairing remains available after a service restart, and the resulting signed
demo token is valid for the configured review window (30 days by default).

## Features to try

* **Cockpit:** Open Cockpit to see “Storefront refresh” working, “Checkout API” waiting, and “Release notes” idle.
  The quota cards are fixed review examples.
* **Decisions:** Open “Publish the preview build?”, choose either answer, and return to Cockpit. The item disappears
  until the hourly demo reset. The credential example demonstrates that sensitive actions are refused.
* **Radio:** Open Radio to see “Harnessgram Review Radio” and its current status briefing. The demo intentionally
  provides metadata without connecting to a private team stream.
* **Widgets:** Add a Harnessgram widget after pairing. It reads the same demo snapshot and can answer only the harmless
  demo decision.
* **Live Activity:** Open the Storefront progress card and request its Live Activity. The demo acknowledges the flow
  without storing a device push token; the in-app progress card remains available for inspection.

All displayed work is fictional. Answers are held only in service memory and reset hourly. Destructive operations,
credentials, messaging, desktop control, and agent execution are unavailable in review mode.
