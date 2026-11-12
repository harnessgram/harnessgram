# Security policy

Harnessgram lets a Telegram account type into Claude Code sessions that can edit files and run commands on
your computer. Treat it with the care you would give a remote shell.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Private vulnerability reporting on GitHub is the
channel for reports: open the repository's **Security** tab and choose **Report a vulnerability**. Include what
you did, what happened, what you expected, and the version (`harnessgram --version` or the commit). Never
include a real bot token, session string or other credential; a redacted example is enough.

## Response policy

Harnessgram is a small open-source project, so these are intentions, not a contract.

- We aim to acknowledge a report within about a week, and to say whether we can reproduce it soon after.
- Fixes are released as soon as they are ready and reviewed; a serious problem that can be exploited remotely is
  handled before other work. The advisory credits you unless you prefer otherwise.
- Please give us reasonable time to fix a problem before you publish details, and tell us if you plan a date.
- Only the latest release (and the `main` branch) receive security fixes.

## Threat model

What the software does today, so you can judge the risk for your own setup. The one-line summary: **the paired
Telegram account is trusted like a user at your keyboard; everything else is treated as untrusted data.**

| Threat | What an attacker gets | What limits it | What you should do |
|---|---|---|---|
| **Stolen or hijacked Telegram session** (someone is logged in as the paired account) | The same power as you over every session: type tasks, answer prompts, approve sensitive actions by typing a yes, read mirrored replies and files. The bridge cannot tell them from you. | Only the paired numeric user id is obeyed, in your home chat or a project group, inside a known session topic (not forwarded, not stale). Forwarded media is accepted only as labelled, untrusted data; the opt-in team group is a separate path where members can only make suggestions. | Turn on Telegram two-step verification and review active sessions. Keep Claude Code's own permission settings strict: that is the layer that stops a command. Set `"mirror": false` if sessions must stay off Telegram: it turns the whole bridge off for every session (see the README, "Privacy"). If the account was exposed, end its other sessions, then revoke the bot token and re-pair. |
| **Bot-token compromise** (the token leaves your machine) | Whoever holds the token can act as the bot: message you, read or consume its updates, and sign a valid Control-panel login, because the panel checks Telegram's `initData` with a key derived from the token. They still cannot be *obeyed* as the owner in a chat, which depends on your Telegram user id. | The token lives in `config.json` (mode 600), its private backup `config.json.bak`, or a command you configure; the state folder is mode 700; the bridge does not print or log the token. An error text returned by a Telegram or local Bot API server is passed on as received, so a server that echoes the request URL could expose the token (`harnessgram bot-api` scrubs it). Credential-looking text sent to the bot in Telegram is refused on the way in (the Cockpit's inputs are not scanned) and redacted on the way out. | Revoke the token in BotFather, put the new one in the config, delete `config.json.bak` (it may hold the old token), restart the listener. Never paste a token into an issue or a shared shell history; `harnessgram bot-api logout` exists so the Bot API migration needs no token on a command line. |
| **Malicious local project** (a repository you open contains hostile files or prompt injection) | It can steer the model in that session, as any repository can with Claude Code. | The hooks never import code from the project: they run the installed command by absolute path (or `python -I`), and detached children start in the private state folder, so a `json.py` in a repository cannot run with access to the token. Messages are fenced as data with a fresh nonce and cannot pose as the framing. | Do not open untrusted repositories in sessions you control from Telegram without Claude Code's permission prompts on. The bridge is not a sandbox. |
| **Cloud translation** (opt-in engines) | The `google` engine (unofficial endpoints) and the `gemini` engine send the prose of replies to Google. Local engines (the default) send nothing. | Both are off unless you add them to `engines`. Credentials are redacted first (unless you set `redact_outgoing` to `false`) and fenced code blocks of a reply are kept out. The `google` engine also replaces inline code, paths and links with placeholders; the `gemini` engine does not, so those inside the prose are sent. The cache is private (mode 600 files). | Leave them off for private work, or use a local engine. See Privacy in the README. |
| **Reverse proxies and public exposure** (the Cockpit, share links) | If you expose the panel on the public internet, anyone can send it requests; only a valid, fresh, owner-signed login gets data (`403` otherwise). A signed login captured in a proxy log can be replayed until it expires (15 minutes to act, 1 hour to read). A shared-file link works for anyone who holds it until it expires (7 days). | The panel binds to loopback, limits bodies, connections and time, refuses chunked bodies, logs nothing and renders data as text only. | Prefer a private network to a public route; forward only the panel's port; never log request headers at the proxy. See `docs/panel.md`. |

### The approval framing is guidance, not enforcement

Every message that reaches a session is wrapped in text that tells the model what it can approve: a typed yes
from the paired owner in a session's topic, or an answer in the Cockpit, approves only the one sensitive action
its own words name; button taps in the Telegram chat, voice notes, pictures, files, forwarded content and team
messages never do. That is an instruction to the model, in the text around each message. It is not a sandbox or
an access-control check, and a model can ignore it (including because of prompt injection). The enforcement
layer is the agent's own permission system.

## What is in scope

- Anyone other than the paired Telegram account getting a message, button or file into a session.
- A message, voice note, file, forwarded post, team-group message or button tap in the Telegram chat being framed
  as an approval when the policy above says it should not be, or an answer in the Cockpit being framed as approving
  more than its own question (see "Approval framing" in the README's Security details).
- The bot token, a Telethon session string, an RTMP key or any other credential reaching a log, a file outside
  the private state folder, a process command line, Telegram or a translation service.
- Code from a project folder being imported or run by the hooks.
- The control panel accepting a request without valid Telegram `initData` from the paired owner.

## Out of scope

- What a Claude Code session does once you, the owner, asked it to, and anything a model does after ignoring the
  framing. Keep Claude Code's own permission settings strict.
- Telegram's own security (bot chats are not end-to-end encrypted) and the security of third-party translation
  or speech services you opt into.
- A compromised computer or user account: the bridge runs with your user's rights.
