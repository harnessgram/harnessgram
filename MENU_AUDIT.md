# Menu audit

Each line denotes a keyboard construction site; empty keyboards that remove buttons are excluded. Cockpit controls are listed separately.

- src/ctb/agents_card.py:206 — render — close
- src/ctb/alerts.py:364 — propose — not-now
- src/ctb/alerts.py:434 — _button — EXEMPT: channel post link, not a menu.
- src/ctb/alerts.py:436 — _button — EXEMPT: channel post link, not a menu.
- src/ctb/alerts.py:438 — _button — EXEMPT: channel post link, not a menu.
- src/ctb/alerts.py:582 — show — close
- src/ctb/alerts.py:630 — button — close
- src/ctb/board.py:325 — _sync — EXEMPT: permanent pinned board; closing would remove its controls.
- src/ctb/board.py:335 — _sync — EXEMPT: permanent pinned board; closing would remove its controls.
- src/ctb/dashboard.py:167 — stuck_check — not-now
- src/ctb/delivery.py:85 — markup — cancel
- src/ctb/delivery.py:95 — cancel_markup — cancel
- src/ctb/dialogs.py:657 — markup — cancel
- src/ctb/inbox.py:921 — send_status — close
- src/ctb/inbox.py:977 — send_mode_menu — close
- src/ctb/inbox.py:1240 — project_picker — cancel
- src/ctb/inbox.py:1280 — _picker_keyboard — cancel
- src/ctb/inbox.py:1327 — default_model_button — close
- src/ctb/inbox.py:1763 — _offer_keyboard — not-now
- src/ctb/inbox.py:2028 — general_offer_button — back
- src/ctb/inbox.py:2219 — skills_menu — close
- src/ctb/inbox.py:2376 — history folders and results — close; empty rows remove the keyboard
- src/ctb/inbox.py:2815 — handle_button — close
- src/ctb/inbox.py:3484 — _keypad_markup — cancel
- src/ctb/inbox.py:3694 — menu_buttons — cancel
- src/ctb/inbox.py:3704 — _keypad — cancel
- src/ctb/inbox.py:4289 — cli_command — close
- src/ctb/inbox.py:4304 — cli_command — not-now
- src/ctb/inbox.py:4337 — run — not-now
- src/ctb/inbox.py:4578 — _stop_now — not-now
- src/ctb/inbox.py:4598 — _stop_without_pane — not-now
- src/ctb/inbox.py:5137 — voice_draft — cancel
- src/ctb/inbox.py:5246 — more_markup — close
- src/ctb/inbox.py:5276 — global_markup — close
- src/ctb/inbox.py:5343 — plugins_page — back / close
- src/ctb/inbox.py:5403 — more_button — cancel
- src/ctb/inbox.py:5413 — more_button — cancel
- src/ctb/inbox.py:5560 — plugins_work — back
- src/ctb/inbox.py:5747 — _resume_markup — not-now
- src/ctb/inbox.py:6413 — account_remove — back
- src/ctb/inbox.py:6434 — account_remove — back
- src/ctb/inbox.py:6523 — account_command — not-now
- src/ctb/inbox.py:6622 — account_command — close
- src/ctb/inbox.py:6671 — effort_menu — close
- src/ctb/members.py:99 — request — not-now
- src/ctb/members.py:114 — member list and retry — close / not-now; empty rows remove the keyboard
- src/ctb/members.py:140 — show — close
- src/ctb/mirror.py:118 — fixed_keyboard — EXEMPT: fixed topic reply keyboard, not a transient menu.
- src/ctb/mirror.py:919 — _yes_no_keyboard — not-now
- src/ctb/mirror.py:2171 — limit_offer — not-now
- src/ctb/moves.py:145 — show_menu — back / close
- src/ctb/moves.py:167 — suggest — cancel
- src/ctb/panel_pin.py:97 — _markup — EXEMPT: permanent pinned topic link, not a menu.
- src/ctb/cockpit_notice.py:66 — update — EXEMPT: one link to the open Cockpit question; the notice closes itself once answered.
- src/ctb/dialogs.py:1506 — _background_card — EXEMPT: one Move to background button; the card clears itself when the command ends or after the tap.
- src/ctb/sessions_menu.py:260 — list_view — close
- src/ctb/sessions_menu.py:286 — detail_view — back
- src/ctb/sessions_menu.py:291 — _confirm — cancel

- src/ctb/telegram.py:151 — full_width — EXEMPT: reformats an existing keyboard without creating a menu.

## Cockpit

- src/ctb/panel_page.py:1237 — YouTube start confirmation — cancel
- src/ctb/panel_page.py:1229 — YouTube project choices (always inline) — EXEMPT: always-visible inline settings, not a dismissible picker.
- src/ctb/panel_page.py:1187 — native date picker — EXEMPT: native date picker is dismissed by the operating system.
- src/ctb/panel_page.py:2043 — bottom bar More sheet — back (its More button toggles it; an outside tap or a tab choice closes it)
- src/ctb/panel_page.py:1044 — session details — back (native details summary toggles)
- src/ctb/panel_page.py:1310 — model choices (always inline) — EXEMPT: always-visible inline settings, not a dismissible picker.
