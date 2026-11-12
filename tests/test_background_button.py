"""A live running-command shortcut is never permission to answer a dialog."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from subprocess import TimeoutExpired
from types import SimpleNamespace

import pytest

from ctb import compact, dialogs as D, inbox as I
from ctb.i18n import t
from test_dialogs import HINT, OWNER, PERMISSION, buttons, last_card, press, rig  # noqa: F401


RUNNING = (Path(__file__).parent / "fixtures" / "cli2.1.322" / "background_running.txt").read_text()
SYNTHETIC = "Bash(sleep 120)\n✻ Running… (21s · ctrl+b to run in background)"
IDLE = "Command moved to background\n❯ "


def test_detection_accepts_real_tmux_capture_with_input_box():
    assert D.background_screen(RUNNING)


@pytest.mark.parametrize("change", ["tool-output", "no-spinner", "no-hint", "two-spinners"])
def test_detection_rejects_inactive_real_capture(change):
    hint = "     (ctrl+b ctrl+b (twice) to run in background)"
    spinner = "✽ Finagling… (24s · ↓ 615 tokens)"
    if change == "tool-output":
        body = RUNNING.replace(hint, hint + "\n  ⎿  More tool output")
    elif change == "no-spinner":
        body = RUNNING.replace(spinner, "")
    elif change == "no-hint":
        body = RUNNING.replace(hint, "")
    else:
        body = RUNNING.replace(spinner, spinner + "\n" + spinner)
    assert not D.background_screen(body)


def test_detection_rejects_transcript_hint_above_current_tool_output():
    hint = "     (ctrl+b ctrl+b (twice) to run in background)"
    body = RUNNING.replace(hint, "  ⎿  More tool output")
    assert not D.background_screen(hint + "\n" + body)


def test_detection_accepts_input_box_without_title_or_status_rows():
    body = "\n".join(RUNNING.splitlines()[:-2])
    body = body.replace(" [redacted] ─", "─────────────")
    body = body.replace("❯ \n", "❯ first input row\n❯ second input row\n")
    assert D.background_screen(body)


@pytest.mark.parametrize("hint", [
    "ctrl+b to run in background",
    "(ctrl+b to run in background)",
    "ctrl+b ctrl+b (twice) to run in background",
    "(ctrl+b ctrl+b (twice) to run in background)",
    "ctrl+b ctrl+b twice to run in background",
    "· ctrl+b to run in background",
    "✻ Running… (21s · ctrl+b to run in background)",
    "⠋ Running… (ctrl+b ctrl+b (twice) to run in background)",
    "\x1b[2m(ctrl+b to run in background)\x1b[0m",
    "(ctrl+b to run in background) · esc to interrupt",
])
def test_detection_accepts_live_hint_variants(hint):
    assert D.background_screen("Bash(sleep 120)\n" + hint)


def test_detection_allows_three_status_rows_and_blanks():
    assert D.background_screen(SYNTHETIC + "\n\n⏵⏵ accept edits on\n50% context\nsonnet\n\n")


@pytest.mark.parametrize("body", [
    "The hint is ctrl+b to run in background",
    "· The documentation says ctrl+b to run in background",
    "✻ Quoted hint: ctrl+b to run in background",
    "echo 'ctrl+b to run in background'",
    "ctrl+b to run in background\nA transcript reply follows",
    "ctrl+b to run in background\n❯ ",
    "ctrl+b to run in background\n────────",
    "ctrl+b to run in background\n╭─────\n│ ❯ continue\n╰─────",
    RUNNING + "\n50% context\nsonnet\nplan mode\n10 tokens",
    "✻ Running… (21s · esc to interrupt)",
    "✻ Working…",
    PERMISSION,
    RUNNING + "\n" + PERMISSION,
    "Do you want to proceed?\n❯ 1. Yes\n  2. No\nctrl+b to run in background",
    "",
])
def test_detection_rejects_transcript_approval_and_plain_spinner(body):
    assert not D.background_screen(body)


@pytest.fixture
def running(rig, monkeypatch):
    rig.scr.body = RUNNING
    monkeypatch.setattr(I, "session_state", lambda session: {"state": "busy"})
    D._first_seen.clear()
    return rig


def watch(rig, now):
    return D.remind(rig.cfg, rig.bot, rig.topics, now=now)


def show(rig):
    assert watch(rig, 1000) == 0
    assert watch(rig, 1020) == 1
    return last_card(rig)


def tap(rig, card, **kw):
    return press(rig, buttons(card)[0]["callback_data"], **kw)


def drops(rig):
    return rig.bot.methods("editMessageReplyMarkup")


def test_card_waits_twenty_seconds_and_is_silent_in_session_topic(running):
    assert watch(running, 1000) == 0
    assert watch(running, 1019.9) == 0
    assert running.bot.methods("sendMessage") == []
    assert watch(running, 1020) == 1
    card = last_card(running)
    assert card["disable_notification"] is True
    assert card["chat_id"] == OWNER and card["message_thread_id"] == running.thread
    assert len(buttons(card)) == 1
    assert buttons(card)[0]["text"] == "Move to background"
    assert t("bg_btn", lang="he") == "העבר לרקע"


def test_one_card_per_command_even_when_spinner_ticks(running):
    show(running)
    running.scr.body = RUNNING.replace("24s", "42s").replace("✽", "✻")
    assert watch(running, 1042) == 0
    assert watch(running, 5000) == 0
    assert len(running.bot.methods("sendMessage")) == 1
    running.scr.body = IDLE
    assert watch(running, 5001) == 0
    running.scr.body = RUNNING.replace("sleep 20", "sleep 40")
    assert watch(running, 5002) == 0
    assert watch(running, 5022) == 1
    assert len(running.bot.methods("sendMessage")) == 2


def test_transient_hint_resets_twenty_second_delay(running):
    assert watch(running, 1000) == 0
    running.scr.body = IDLE
    assert watch(running, 1010) == 0
    running.scr.body = RUNNING
    assert watch(running, 1011) == 0
    assert watch(running, 1030) == 0
    assert watch(running, 1031) == 1


def test_running_hint_never_opens_typed_answer_mode(running):
    assert D.open_screen("s")[3] is False
    assert not D.bottom_hint(RUNNING)
    assert not I._dialog_open(RUNNING, "s")
    assert not D.send_card(running.cfg, running.bot, "s", running.thread)
    show(running)
    assert not D.rec_get("s").get("typed")
    assert not D.pending_for("s", {"text": "1", "message_thread_id": running.thread}, chat=OWNER)


def test_loose_dialog_blocks_background_card_but_keeps_dialog_card(running):
    running.scr.body = "1: Continue  2: Cancel\n" + RUNNING
    assert D.background_screen(running.scr.body)
    assert D.parse(running.scr.body) is None
    assert D.open_screen("s")[3] is False
    assert I._dialog_open(running.scr.body, "s")
    assert watch(running, 1000) == 0
    assert watch(running, 1020) == 0
    assert D.send_card(running.cfg, running.bot, "s", running.thread)
    assert D.rec_get("s")["loose"] is True
    assert all(not b["callback_data"].startswith("dg:b:") for b in buttons(last_card(running)))


@pytest.mark.parametrize("at_claim", [False, True])
def test_loose_dialog_blocks_tap_at_both_screen_checks(running, monkeypatch, at_claim):
    card = show(running)
    if at_claim:
        update = D._update

        def claim_then_dialog(fn):
            result = update(fn)
            running.scr.body = "1: Continue  2: Cancel\n" + RUNNING
            return result

        monkeypatch.setattr(D, "_update", claim_then_dialog)
    else:
        running.scr.body = "1: Continue  2: Cancel\n" + RUNNING
    tap(running, card)
    assert running.scr.sent == []
    assert not D.rec_get("s").get("consumed")


@pytest.mark.parametrize("suppression", ["compaction", "moving"])
def test_background_card_is_suppressed_during_compaction_or_move(running, monkeypatch, suppression):
    if suppression == "compaction":
        monkeypatch.setattr(compact, "just_started", lambda s: True)
    else:
        monkeypatch.setattr(I, "recently_moving", lambda s: True)
    assert watch(running, 1000) == 0
    assert watch(running, 1020) == 0
    assert running.bot.methods("sendMessage") == []


@pytest.mark.parametrize("suppression", ["compaction", "moving"])
@pytest.mark.parametrize("at_claim", [False, True])
def test_background_tap_is_suppressed_at_both_screen_checks(running, monkeypatch, suppression, at_claim):
    card = show(running)

    def suppress():
        if suppression == "compaction":
            monkeypatch.setattr(compact, "just_started", lambda s: True)
        else:
            monkeypatch.setattr(I, "recently_moving", lambda s: True)

    if at_claim:
        update = D._update

        def claim_then_suppress(fn):
            result = update(fn)
            suppress()
            return result

        monkeypatch.setattr(D, "_update", claim_then_suppress)
    else:
        suppress()
    tap(running, card)
    assert running.scr.sent == []
    assert not D.rec_get("s").get("consumed")


def test_approval_dialog_has_priority_and_drops_background_keyboard(running):
    show(running)
    running.scr.body = RUNNING + "\n" + PERMISSION
    assert watch(running, 1030) == 0
    assert drops(running)[-1]["reply_markup"] == {"inline_keyboard": []}
    assert watch(running, 1050) == 1
    assert all(not b["callback_data"].startswith("dg:b:") for b in buttons(last_card(running)))
    assert D.rec_get("s")["typed"] is True


def test_keyboard_dropped_when_hint_disappears_even_if_session_is_idle(running, monkeypatch):
    show(running)
    mid = D.rec_get("s")["message_id"]
    running.scr.body = IDLE
    monkeypatch.setattr(I, "session_state", lambda session: {"state": "idle"})
    assert watch(running, 1030) == 0
    assert drops(running)[-1]["message_id"] == mid
    assert drops(running)[-1]["reply_markup"] == {"inline_keyboard": []}
    assert D.rec_get("s") == {}


def test_stale_tap_updates_card_without_consuming_or_sending_keys(running):
    card = show(running)
    running.scr.body = IDLE
    tap(running, card)
    assert running.scr.sent == []
    assert not D.rec_get("s").get("consumed")
    assert running.bot.methods("editMessageText")[-1]["text"] == t("bg_card_finished", lang="en")
    assert drops(running)[-1]["reply_markup"] == {"inline_keyboard": []}
    assert running.bot.methods("answerCallbackQuery")[-1]["text"] == t("bg_toast_stale", lang="en")


def test_tap_sends_exactly_one_control_b_and_drops_keyboard(running, monkeypatch):
    card = show(running)
    running.scr.after["C-b"] = IDLE
    calls = []
    original = running.scr.tmux

    def tmux(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(I, "_tmux", tmux)
    tap(running, card)
    assert calls == [("send-keys", "-t", "%1", "C-b")]
    assert running.scr.sent == ["C-b"]
    assert drops(running)[-1]["reply_markup"] == {"inline_keyboard": []}
    assert not D.rec_get("s").get("typed")
    assert running.bot.methods("answerCallbackQuery")[-1]["text"] == t("bg_toast_done", lang="en")
    assert running.bot.methods("editMessageText")[-1]["text"] == t("bg_toast_done", lang="en")
    tap(running, card)
    assert running.scr.sent == ["C-b"]


def test_failed_verification_sends_no_extra_keys_or_replacement_card(running):
    card = show(running)
    tap(running, card)  # the simulated pane keeps showing the hint
    assert running.scr.sent == ["C-b"]
    assert running.bot.methods("answerCallbackQuery")[-1]["text"] == t("bg_toast_failed", lang="en")
    assert running.bot.methods("editMessageText")[-1]["text"] == t("bg_toast_failed", lang="en")
    assert drops(running)[-1]["reply_markup"] == {"inline_keyboard": []}
    tap(running, card)
    assert running.scr.sent == ["C-b"]
    assert watch(running, 5000) == 0


def test_verification_waits_for_hint_to_disappear(running, monkeypatch):
    card = show(running)
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        assert D.rec_get("s")["consumed"] is True
        if len(waits) == 3:
            running.scr.body = IDLE

    monkeypatch.setattr(D.time, "sleep", sleep)
    tap(running, card)
    assert waits == [0.5, 0.5, 0.5]
    assert running.scr.sent == ["C-b"]
    assert running.bot.methods("answerCallbackQuery")[-1]["text"] == t("bg_toast_done", lang="en")


@pytest.mark.parametrize("after", ["", "missing-pane", "different-pane"])
def test_missing_screen_or_changed_pane_is_not_success(running, monkeypatch, after):
    card = show(running)

    def tmux(*args):
        running.scr.sent.extend(args[3:])
        running.scr.body = "" if after == "" else IDLE
        if after != "":
            monkeypatch.setattr(I, "pane_for_session", lambda s: None if after == "missing-pane" else "%2")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(I, "_tmux", tmux)
    tap(running, card)
    assert running.scr.sent == ["C-b"]
    assert running.bot.methods("answerCallbackQuery")[-1]["text"] == t("bg_toast_failed", lang="en")


@pytest.mark.parametrize("failure", [None, OSError("pane unavailable"), TimeoutExpired("tmux", 10)])
def test_tmux_failure_leaves_command_retryable_and_updates_card(running, monkeypatch, failure):
    card = show(running)

    def tmux(*args):
        if failure is not None:
            raise failure
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(I, "_tmux", tmux)
    tap(running, card)
    assert running.bot.methods("answerCallbackQuery")[-1]["text"] == t("bg_toast_failed", lang="en")
    assert drops(running)[-1]["reply_markup"] == {"inline_keyboard": []}
    assert not D.rec_get("s").get("consumed")
    assert running.bot.methods("editMessageText")[-1]["text"] == t("bg_toast_failed", lang="en")
    monkeypatch.setattr(I, "_tmux", running.scr.tmux)
    assert watch(running, 1030) == 1
    running.scr.after["C-b"] = IDLE
    tap(running, last_card(running))
    assert running.scr.sent == ["C-b"]
    assert D.rec_get("s")["consumed"] is True


@pytest.mark.parametrize("attacker", ["owner", "bot", "chat", "topic", "message", "stamp", "session"])
def test_tap_is_bound_to_paired_owner_and_session_card(running, attacker):
    card = show(running)
    mid = D.rec_get("s")["message_id"]
    m = {"chat": {"id": OWNER}, "message_thread_id": running.thread, "message_id": mid}
    q = {"id": "q", "from": {"id": OWNER}, "message": m, "data": buttons(card)[0]["callback_data"]}
    session = "s"
    if attacker == "owner":
        q["from"]["id"] = OWNER + 1
    elif attacker == "bot":
        q["from"]["is_bot"] = True
    elif attacker == "chat":
        m["chat"]["id"] = OWNER + 1
    elif attacker == "topic":
        m["message_thread_id"] += 1
    elif attacker == "message":
        m["message_id"] += 1
    elif attacker == "stamp":
        q["data"] = "dg:b:" + "0" * 12
    else:
        session = "other"
    assert D.button(q, running.cfg, running.bot, m, session) in ("ignored", "stale")
    assert running.scr.sent == []


def test_old_card_cannot_background_a_later_command(running):
    old = show(running)
    old_mid = D.rec_get("s")["message_id"]
    running.scr.body = IDLE
    watch(running, 1030)
    running.scr.body = RUNNING
    watch(running, 1040)
    watch(running, 1060)
    tap(running, old, message_id=old_mid)
    assert running.scr.sent == []


@pytest.mark.parametrize("at_claim", [False, True])
def test_command_change_without_idle_screen_rejects_old_card(running, monkeypatch, at_claim):
    card = show(running)
    replacement = RUNNING.replace("sleep 20", "sleep 40")
    if at_claim:
        update = D._update

        def claim_then_change(fn):
            result = update(fn)
            running.scr.body = replacement
            return result

        monkeypatch.setattr(D, "_update", claim_then_change)
    else:
        running.scr.body = replacement
    tap(running, card)
    assert running.scr.sent == []
    assert not D.rec_get("s").get("consumed")


def test_new_command_gets_card_after_consumed_command_without_idle_screen(running):
    show(running)
    card = last_card(running)
    tap(running, card)  # the hint remains, so verification cannot confirm the move
    old = D.rec_get("s")
    assert old["consumed"] is True
    running.scr.body = RUNNING.replace("sleep 20", "sleep 40")
    assert watch(running, 1030) == 0
    assert watch(running, 1050) == 1
    new = D.rec_get("s")
    assert new["fingerprint"] != old["fingerprint"]
    assert new["stamp"] != old["stamp"]
    assert not new.get("consumed")


def test_command_fingerprint_ignores_styling_spacing_and_spinner_ticks(running):
    show(running)
    fingerprint = D.rec_get("s")["fingerprint"]
    running.scr.body = RUNNING.replace("⏺ Bash(sleep 20; echo done)",
                                       "\x1b[2m  ●  Bash(sleep   20; echo done)\x1b[0m")
    running.scr.body = running.scr.body.replace("24s", "42s")
    assert D._background_fingerprint(running.scr.body) == fingerprint
    assert watch(running, 1042) == 0
    assert len(running.bot.methods("sendMessage")) == 1


def test_final_stale_read_releases_claim_and_allows_retry(running, monkeypatch):
    card = show(running)
    read = D.open_screen
    reads = []

    def screen(session):
        reads.append(session)
        if len(reads) == 2:
            return "%1", IDLE, None, False
        return read(session)

    monkeypatch.setattr(D, "open_screen", screen)
    tap(running, card)
    assert running.scr.sent == []
    assert not D.rec_get("s").get("consumed")
    assert not D.rec_get("s").get("sending")
    assert watch(running, 1030) == 1
    running.scr.after["C-b"] = IDLE
    tap(running, last_card(running))
    assert running.scr.sent == ["C-b"]


def test_failed_final_read_releases_claim_without_consuming_card(running, monkeypatch):
    card = show(running)
    read = D.open_screen
    reads = []

    def screen(session):
        reads.append(session)
        if len(reads) == 2:
            raise OSError("capture unavailable")
        return read(session)

    monkeypatch.setattr(D, "open_screen", screen)
    tap(running, card)
    assert running.scr.sent == []
    assert not D.rec_get("s").get("consumed")
    assert not D.rec_get("s").get("sending")
    assert running.bot.methods("editMessageText")[-1]["text"] == t("bg_toast_failed", lang="en")
    assert watch(running, 1030) == 1


def test_stale_tap_cannot_release_another_taps_claim(running, monkeypatch):
    card = show(running)
    old = D.rec_get("s")
    current = dict(old, sending="another-claim", consumed=True)
    D.rec_set("s", current)
    rec_get = D.rec_get
    monkeypatch.setattr(D, "rec_get", lambda s: old)
    monkeypatch.setattr(D, "open_screen", lambda s: ("%1", IDLE, None, False))
    q = {"id": "q", "data": buttons(card)[0]["callback_data"]}
    m = {"message_id": old["message_id"]}
    assert D._background_button(q, running.cfg, running.bot, m, "s") == "stale"
    assert running.scr.sent == []
    assert rec_get("s") == current
    assert running.bot.methods("editMessageText") == []


def test_permission_appearing_before_tap_prevents_any_key(running):
    card = show(running)
    running.scr.body = PERMISSION
    tap(running, card)
    assert running.scr.sent == []


def test_permission_appearing_during_claim_prevents_any_key(running, monkeypatch):
    card = show(running)
    update = D._update

    def claim_then_permission(fn):
        result = update(fn)
        running.scr.body = PERMISSION
        return result

    monkeypatch.setattr(D, "_update", claim_then_permission)
    tap(running, card)
    assert running.scr.sent == []
    assert drops(running)[-1]["reply_markup"] == {"inline_keyboard": []}
    assert not D.rec_get("s").get("consumed")


def test_concurrent_taps_send_only_one_control_b(running):
    card = show(running)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: tap(running, card), range(2)))
    assert running.scr.sent == ["C-b"]
    assert D.rec_get("s")["consumed"] is True
    assert not D.rec_get("s").get("typed")


def test_card_uses_project_group_and_drops_keyboard_there(running, monkeypatch):
    entry = {"thread": running.thread, "chat": OWNER + 10, "home": False}
    monkeypatch.setattr(running.topics, "all_places", lambda: {"s": entry})
    monkeypatch.setattr(D, "place", lambda cfg, s: SimpleNamespace(chat=entry["chat"], thread=entry["thread"]))
    card = show(running)
    assert card["chat_id"] == entry["chat"]
    running.scr.after["C-b"] = IDLE
    m = {"chat": {"id": entry["chat"]}, "message_thread_id": running.thread,
         "message_id": D.rec_get("s")["message_id"]}
    q = {"id": "q", "from": {"id": OWNER}, "message": m, "data": buttons(card)[0]["callback_data"]}
    assert D.button(q, running.cfg, running.bot, m, "s") == "background"
    assert drops(running)[-1]["chat_id"] == entry["chat"]


def test_detection_accepts_task_list_under_the_spinner():
    """Claude Code draws its task list (and tips) between the spinner and the input box."""
    from pathlib import Path
    from ctb import dialogs
    body = (Path(__file__).parent / "fixtures" / "cli2.1.322" / "background_running.txt").read_text()
    rows = body.splitlines()
    spin = next(i for i, r in enumerate(rows) if "Finagling" in r or "…" in r and "s ·" in r)
    tasks = ["  ⎿  ◼ Run the suite", "     ☐ Install the batch", "     ✔ Build the branch", "      … +2 pending"]
    with_tasks = "\n".join(rows[:spin + 1] + tasks + rows[spin + 1:])
    assert dialogs.background_screen(with_tasks)
    prose = "\n".join(rows[:spin + 1] + ["  some reply text that is not a task row"] + rows[spin + 1:])
    assert not dialogs.background_screen(prose)
