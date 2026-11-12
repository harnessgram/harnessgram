# Cockpit answer delivery (PR 239 second pass)

An HTTP success means the answer was saved, not that the model received it. These
regressions were reproduced with sample queues, panes and transcripts. They prove
failure mechanisms; they do not identify the cause of any historical live incident.

## Complete path

1. `PanelHandler._say`, `_card`, and `_decide` validate the request/session, then
   `say_to`, `cards.answer`, or `decisions.answer` put a `source: panel` item in the
   session's inbox. Card/decision records retain its delivery key. A refused write
   leaves the question open. A full panel queue returns `409 / queue full`.
2. `cards.wake` / `decisions.announce` schedule `_deliver_button_word`'s wake even
   when the saved state says busy. A live waiter gets the first chance to take it.
   Without one, `_nudge_without_waiter` checks the actual empty, idle input box,
   verifies the pane still hosts this session, and verifies submission. An active
   turn, permission/question dialog, another session's pane or owner draft is
   never typed into. The saved busy mark alone cannot veto an idle screen.
3. The wake waiter, prompt/step/Stop hook takes items into a `.lease`, and its
   output carries the lease nonce. Taking alone means `in_flight`, not delivered.
4. The listener's sweeper retries queued answers, checks trusted transcript
   records, settles proven leases, and puts discarded output back ahead of newer
   answers after the existing idle/dead-output checks. A missing transcript must
   not let a newer answer overtake an unproven panel lease. Timeout alone never
   replays an answer while its original output could still reach the model.
5. `build_state` exposes recent per-answer receipts from the queue, leases and
   settled proof keys. The page updates answered cards/decisions on subsequent
   polls and retains receipts across a page reload. Free-text messages stay
   visible while in flight; edit/cancel are available only while queued. Retry
   responses include the original key's actual delivery state.

## Findings and guards

| Finding | Change | Regression guards |
| --- | --- | --- |
| Full panel queue raises into Telegram handling and blocks the cursor | Only new panel sends are refused. Other sources keep the bounded drop-note behavior. Already accepted, bounded limit replays remain retainable. | `test_full_panel_queue_never_raises_from_handle_or_blocks_the_next_update` (owner text and Stop); `test_previously_accepted_panel_replay_is_not_consumed_and_lost_at_capacity` |
| Stale busy state prevents any nudge | Schedule the guarded nudge regardless of the saved busy mark. A genuinely busy session takes the answer through its next hook. | `test_idle_screen_without_waiter_gets_answer_even_with_stale_busy_mark` (all three endpoints); busy/dialog/draft/waiter guards |
| Wake errors are swallowed | Record a fixed, private failure marker and safe exception type; show retry status while the accepted answer remains pending. The sweeper retries and clears the marker on a successful nudge. Optional progress-boundary errors are logged without undoing a saved answer. | `test_wake_error_is_visible_and_sweeper_retries_the_saved_answer`; terminal-wake and optional-boundary guards |
| One session blocks another's wake | Remove the process-wide nudge lock. Use the existing per-session file lock without blocking, including the resume helper. | `test_one_slow_session_never_serializes_another_sessions_nudge`; external-lock and two-session delivery guards |
| Lease is invisible or newer answers overtake it | Expose in-flight messages/receipts; keep later answers queued while a panel lease has no transcript. Existing safe sweeper recovery releases it in order. | `test_discarded_lease_visible_then_put_back_before_newer_answer`; `test_missing_transcript_does_not_let_new_answers_overtake_a_lease` |
| Waiting label never becomes delivered; retry/full refusal misleads | Return/expose the delivery key and proof-based state for all three endpoints. Refresh saved receipts and local answered views; show a distinct full-queue conflict while preserving the draft/question. | `test_receipt_changes_only_after_transcript_proof`; retry/conflict/state-poll guards; executed page-function tests in `test_cockpit_delivery_ui.py` |
| Drop notice overcounts panel put-backs | Count the items actually discarded, preserving all panel items and any earlier drop count. | `test_put_back_over_capacity_does_not_invent_dropped_messages` |

The move/replay test panes now model the actual input box and consumption on Enter,
so those tests exercise the stricter submission check rather than an unreadable
screen. Existing tests expecting the harmful owner exception or generic `500`
were corrected.

## Evidence and limits

The new guards are in `tests/test_cockpit_delivery_path.py` and
`tests/test_cockpit_delivery_ui.py`. All 45 pass on the repaired code. Against the
committed branch head before this pass, 37 fail and 8 existing-behavior safety
checks pass. This comparison used a temporary source export inside this worktree,
not another worktree or the installed package. With pytest available, run:

```sh
python -m pytest -q tests/test_cockpit_delivery_path.py tests/test_cockpit_delivery_ui.py
```

The broader targeted selection also covers the existing queue, panel, cards,
decisions, hooks, sweeper, inbox, limit recovery and account-move regression tests.
The final targeted run reports **365 passed, 71 skipped**: 70 skipped cases need
a loopback HTTP server and one needs the real process table. There are no new
focused lint diagnostics compared with the committed baseline (the pre-existing
unused `t_en` import remains). `git diff --check` passes.

The restricted environment cannot bind a loopback HTTP server or run the real
process-table probe. Its socket-dependent tests must run in a permitted environment;
the new endpoint guards call the handlers directly and the page tests execute the
actual JavaScript functions in Node without network access.

Receipts show up to 60 recent sends within 24 hours. Settled keys retain the existing
500-key bound. Missing/expired proof is `unknown`, never an inferred delivery.
Legacy answers without a delivery key cannot acquire a retrospective receipt.
The radio's lightweight state intentionally omits receipt history. No live session,
installed tool, listener job, production data or remote PR state was validated.
