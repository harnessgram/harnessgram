# What HarnessGram adds to your agents

<!-- Generated from src/ctb/addons.py by `harnessgram addons docs`. Do not edit by hand. -->

Every behaviour the bridge adds into or around an agent harness, with its switch. The Cockpit shows
them under Settings; a switch takes effect at once. Locked safety rows are shown and never switched
off there. An add-on that already had a setting keeps it: the switch here is that same setting.

## Conversation

| Key | Add-on | What it does | Default | Switch | Applies to |
|---|---|---|---|---|---|
| `reply_mirror` | The bridge itself: replies in Telegram | The bridge's master switch. It changes only in the config file on the computer, so the Cockpit never switches itself off. | always on | locked (config `mirror`) | Claude, Codex |
| `translation` | Translation and the Summary line | The agent writes in English and ends with a Summary line; you get the reply in your language. | off | config `translate` | Claude |
| `context_footer` | Context size line | A small line under each reply shows how much of the context is in use. | on | config `context_footer` | Claude |
| `reply_index` | Pinned reply index | A pinned message in each topic links to the latest replies. | on | config `pin_replies` | Claude |
| `missed_replies` | Missed reply redelivery | If an agent's last reply never reached Telegram, the bridge sends it again. | on | `addons.json` | Claude |
| `topic_titles` | Automatic topic names | The bridge names each topic after its project and task and keeps the name current. | off | config `auto_topic_names` | Claude |
| `title_sync` | Session title from the topic | The Telegram topic's name also becomes the session's title in the agent. | on | `addons.json` | Claude |
| `progress_cards` | Live work card | One message updates with the agent's latest steps while it works. | off | config `progress` | Claude |
| `progress_reminder` | Progress card reminder | On a long task the bridge reminds the agent to open a progress card in the Cockpit. | on | `addons.json` | Claude |
| `cockpit_hint` | Cockpit tools guide | The bridge teaches the agent the pin board, the cards and yes-or-no questions. | on | `addons.json` | Claude |
| `rules_reminder` | Your standing reminder | Your standing sentence and the periodic reminder are added to the agent's prompts. | on | `addons.json` | Claude |

## Memory and continuity

| Key | Add-on | What it does | Default | Switch | Applies to |
|---|---|---|---|---|---|
| `knowledge_overlay` | Switched-off items list | Agents receive the list of rules and tools you switched off in “What the system knows”. | on | `addons.json` | Claude, Codex |
| `auto_compact` | Automatic compaction | When a conversation grows too long, the bridge compacts it between tasks. | off | Cockpit setting `auto_compact` | Claude |
| `cold_compact` | Compaction after a break | After an hour of quiet, the bridge compacts a large conversation before your next message. | on | Cockpit setting `cold_compact` | Claude |
| `compact_helper` | Agent-requested compaction | The agent can ask for a compaction at the end of a task, naming what must be kept. | on | `addons.json` | Claude |
| `recall_after_compact` | Recall after compaction | After a compaction the agent is reminded of what matters. | on | `addons.json` (gate in `feat/recall-after-compact`) | Claude |
| `rule_change_notice` | Rule change notice | When a rule changes, the active agents hear about it right away. | on | `addons.json` (gate in `feat/recall-after-compact`) | Claude |

## Voice and radio

| Key | Add-on | What it does | Default | Switch | Applies to |
|---|---|---|---|---|---|
| `radio_replies` | The radio reads every reply | The radio says every reply aloud, not only answers to voice notes. | on | Cockpit setting `radio_every_reply` | Claude |
| `radio_group_live` | Live stream in the group | The radio opens a live stream with a picture in the group. Off: listening is private only, in the app player and the Safari link, and the computer works less. A change applies the next time the radio starts. | on | `addons.json` | Claude, Codex |
| `voice_digest` | Voice summaries | The bridge gathers replies into a private voice summary, outside quiet hours. | on | Cockpit setting `voice_digest` | Claude |
| `car_mode` | Car mode | Summaries arrive with an alert so they are read aloud, and dictations go to the last session. | off | state file `car-mode` | Claude |
| `car_voice` | Read aloud in car mode | In car mode the summary is also sent as a voice message. | on | config `voice` | Claude |
| `voice_echo` | Echo when sent without a preview | A recording that goes to the agent without the transcript preview (the driving window, car mode set to send directly, or a preview Telegram never showed) gets a quiet reply with what was heard and why. | on | `addons.json` | Claude |
| `listen_link` | Listen in Safari | A radio button opens a private link to a listening page in Safari, which keeps playing in the background and on the lock screen. | on | `addons.json` | Claude, Codex |
| `radio_agent_media` | Agent media on the radio | An agent can put an audio or video file it made in your private radio queue: with one tap, or automatically while car mode is on. | on | `addons.json` | Claude, Codex |
| `listen_button` | Listen button | A button under a reply plays it aloud. | on | `addons.json` (gate in `feat/listen-button`) | Claude |

## Cockpit

| Key | Add-on | What it does | Default | Switch | Applies to |
|---|---|---|---|---|---|
| `morning_brief` | Morning Brief | The Cockpit home shows what changed and what waits for you. | off | Cockpit setting `brief_home` | Claude, Codex |
| `home_context` | Home mode | A small chip explains which context currently guides the home screen. | off | `addons.json` | Claude, Codex |
| `schedule_card` | My schedule | A Today and Tomorrow card on Now, with your private list. | on | `addons.json` | Claude, Codex |
| `work_map` | Work map | Each project gets a map of what the agents are doing, what waits for you and what it changes for you: in the Cockpit, in a pinned message and with /what. | on | `addons.json` | Claude, Codex |
| `morning_briefing` | Morning briefing | At your morning time a short briefing arrives: the schedule, what finished overnight and what waits for you. A Cockpit card, a message and, when the radio is on, spoken on the private stream. | on | `addons.json` | Claude, Codex |
| `app_request` | Request an app | One button sends an app link for approval. | on | `addons.json` | Claude, Codex |
| `mac_tools` | Mac maintenance | Clean and maintain the Mac in one tap, after a preview. | on | `addons.json` | Claude, Codex |
| `topic_cleanup` | Old topic cleanup | The Cockpit offers to close topics that have been quiet for a long time. | on | `addons.json` | Claude, Codex |

## Safety (locked)

| Key | Add-on | What it does | Default | Switch | Applies to |
|---|---|---|---|---|---|
| `approval_gates` | Approval for sensitive actions | A sensitive action waits for your approval and is never approved by itself. | always on | locked | Claude |
| `secret_redaction` | Secret redaction | Anything that looks like a password or a key is replaced before text leaves the computer. | always on | locked (config `redact_outgoing`) | Claude, Codex |
| `owner_only` | Owner-only control | Only your account can approve, change settings and write to the agents. | always on | locked | Claude, Codex |
| `sensitive_wording` | Exact wording for sensitive questions | A question about a sensitive action must name exactly one action, and its card is marked red. | always on | locked | Claude |
