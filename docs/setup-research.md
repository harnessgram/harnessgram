# Setup research: can Harnessgram create the bot for you?

Question: can `harnessgram setup` skip the manual BotFather steps (create a bot, copy its token, switch on
Threaded Mode) by using Telegram's "managed bots", a bot that creates bots for a user?

Sources: the official Bot API reference (core.telegram.org/bots/api), its changelog
(core.telegram.org/bots/api-changelog) and the features page (core.telegram.org/bots/features, section
"Managed Bots"). Only names that appear there are used below.

## What exists

Managed bots arrived in Bot API 9.6 and were extended in 10.0 and 10.4.

| Piece | What the documentation says |
|---|---|
| Manager switch | A bot becomes a manager when its owner enables "Bot Management Mode" in the BotFather Mini App. `getMe` then reports `User.can_manage_bots`. |
| Creation link | `https://t.me/newbot/{manager_bot_username}/{new_username}?name={new_name}`: the user sees a window with the username and name filled in (editable) and confirms. |
| Creation button | `KeyboardButton.request_managed_bot` with `KeyboardButtonRequestManagedBot` (`request_id`, `suggested_name`, `suggested_username`), private chats only. Mini Apps can use `savePreparedKeyboardButton`. |
| Notice to the manager | `Update.managed_bot` (`ManagedBotUpdated`: `user`, `bot`) and the service message `Message.managed_bot_created` (`ManagedBotCreated`: `bot`). |
| Token | `getManagedBotToken(user_id)` returns the new bot's token to the manager; `replaceManagedBotToken(user_id)` revokes it and makes a new one. |
| Access | `getManagedBotAccessSettings` / `setManagedBotAccessSettings` (`BotAccessSettings`: `is_access_restricted`, `added_users`). |
| Topics check | `getMe` returns `has_topics_enabled` ("forum topic mode enabled in private chats") and `allows_users_to_create_topics`. |

## What it would and would not save

- **Saved:** typing `/newbot`, a name and a free username in BotFather (one confirm tap instead).
- **Not saved:** Threaded Mode. The Bot API has a way to read it (`has_topics_enabled`) but no method to switch
  it on; the features page says private-chat topics "need to be enabled for the bot via @BotFather". The user
  still opens BotFather once.
- **Not saved:** the token handoff. Only the manager can call `getManagedBotToken`. The new bot's token reaches
  the user's computer only through the manager: a hosted service, or a message the user copies anyway.

## Why Harnessgram does not use it

1. **Someone else keeps your keys.** The manager can read (`getManagedBotToken`) and replace
   (`replaceManagedBotToken`) the token of every bot it created, at any time. Harnessgram's security model is
   that the token lives only on your computer; a manager breaks that.
2. **It needs a server.** The manager must be online to receive `managed_bot` and fetch the token. Harnessgram is
   self-hosted and runs no shared service.
3. **The visit to BotFather stays** for Threaded Mode, so the gain is one typed name and username.

Harnessgram could offer it later as an opt-in "invite a friend" flow, where an existing user's own bot is the
manager and both sides accept that it keeps access to the friend's token. That needs its own security review.

## What setup does instead

- Prints a ready name and a random username BotFather is likely to accept first time (`harnessgram_…_bot`);
  the setup page shows the same.
- Reads the token from the clipboard when you press Enter at the hidden prompt (tap the token in BotFather's
  message to copy it, or copy the whole message), then clears the clipboard. Typing or pasting still works.
- Finds the token in BotFather's whole message, in the terminal (a multi-line paste is read as one block) and on
  the setup page, and refuses text with two different tokens, or text too long to check, instead of guessing.
- Never leaves the token on the clipboard without saying so: a clipboard that held a token is cleared (also when
  the text was refused, and after a hand-pasted token was saved); a failed clear is reported plainly. The setup
  page ends with a one-tap "Clear the clipboard" button.
- Checks Threaded Mode with `getMe` `has_topics_enabled` and waits while you switch it on (as before).
- Sets the bot's command menu itself once the listener runs (`setMyCommands`, as before).

The token is never printed or logged, and it is stored as before (config file, mode 600, or your token command).
