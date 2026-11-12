"""Tap answers need fresh hook proof, exact text and a single-use owner/topic-bound card."""
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ctb import asks, dialogs as D, hooks as H, install
from test_dialogs import (HINT, OWNER, buttons, last_card, only_utility_buttons, press, rig)  # noqa: F401

QUESTION = "Continue with the lighter model?"
LABELS = ["Yes, switch model", "No, go back"]


def tool_input(question=QUESTION, labels=None):
    return {"questions": [{"question": question, "options": [{"label": x} for x in (labels or LABELS)],
                           "multiSelect": False}]}


def screen(question=QUESTION, labels=None):
    labels = labels or LABELS
    return f"{question}\n\n❯ 1. {labels[0]}\n  2. {labels[1]}\n\n{HINT}"


def hook(question=QUESTION, labels=None, session="s", **extra):
    return H.hook_ask(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                                  "session_id": session, "tool_input": tool_input(question, labels), **extra}))


def show(rig, body=None):
    rig.scr.body = screen() if body is None else body
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    return last_card(rig)


def tap(card, number=1):
    return next(b["callback_data"] for b in buttons(card) if b["callback_data"].startswith(f"dg:o:{number}:"))


def test_recorded_regular_question_gets_one_button_per_option_and_uses_typed_answer_path(rig, monkeypatch):
    assert hook() == 0
    calls = []
    original = D._answer

    def answer(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(D, "_answer", answer)
    card = show(rig)
    assert [b["text"] for b in buttons(card)[:2]] == [f"{i}. {x}" for i, x in enumerate(LABELS, 1)]
    press(rig, tap(card, 2))
    assert rig.scr.sent == ["2"] and len(calls) == 1 and calls[0][-1] == 2


def test_without_hook_proof_stays_typed_only(rig):
    assert only_utility_buttons(show(rig))
    press(rig, f"dg:o:1:{D.parse(screen()).stamp}")
    assert rig.scr.sent == []


@pytest.mark.parametrize("header", ["☐ Question", "←  ☐ Question  ✔ Submit  →"])
def test_form_header_without_blank_row_is_not_part_of_question(rig, header):
    hook()
    card = show(rig, header + "\n" + screen())
    assert D.parse(rig.scr.body).question_text == QUESTION
    press(rig, tap(card))
    assert rig.scr.sent == ["1"]


def test_multiselect_without_blank_header_keeps_toggle_and_submit(rig):
    from multisim import Sim
    rig.scr.sim = sim = Sim()
    asks.record("s", {"questions": [{"question": "Which toppings do you want?", "multiSelect": True,
                                    "options": [{"label": x} for x in ["Cheese", "Olives", "Mushrooms"]]}]})
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    card = last_card(rig)
    toggle = next(b["callback_data"] for b in buttons(card) if b["callback_data"].startswith("dg:t:1:"))
    press(rig, toggle)
    refreshed = rig.bot.methods("editMessageText")[-1]
    done = next(b["callback_data"] for b in buttons(refreshed) if b["callback_data"].startswith("dg:d:"))
    press(rig, done)
    assert sim.done == ["Cheese"]


def test_preview_without_blank_header_keeps_verified_selection(rig):
    from multisim import PreviewSim
    rig.scr.sim = sim = PreviewSim()
    hook("Which layout do you prefer?", ["Compact", "Roomy"])
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    press(rig, tap(last_card(rig), 2))
    assert sim.done == "Roomy" and rig.scr.sent == ["Down", "Enter"]


def test_unrecorded_extra_option_cannot_hide_behind_a_builtin_prefix(rig):
    hook()
    body = screen().replace("\n\n" + HINT, "\n  3. Skip and choose a different action\n\n" + HINT)
    assert only_utility_buttons(show(rig, body))


@pytest.mark.parametrize("prefix", ["Do not answer ← choose →", "A different question\n←  ☐ Question  ✔ Submit  →"])
def test_question_prose_cannot_be_stripped_as_form_navigation(rig, prefix):
    hook()
    assert only_utility_buttons(show(rig, prefix + "\n" + screen()))


@pytest.mark.parametrize("question,labels", [
    ("Publish the result?", ["Yes", "No"]),
    (QUESTION, ["Always use this model", "No"]),
    ("Allow this for this session?", ["Yes", "No"]),
    ("Do you want to proceed?", ["Yes", "No"]),
    ("Ready to code?", ["Yes", "No"]),
    ("Select model", ["Fast", "Careful"]),
])
def test_sensitive_lasting_permission_plan_and_menu_stay_typed_only(rig, question, labels):
    hook(question, labels)
    body = screen(question, labels)
    assert only_utility_buttons(show(rig, body))
    press(rig, f"dg:o:1:{D.parse(body).stamp}")
    assert rig.scr.sent == []


def test_expired_proof_hides_buttons_and_rejects_old_tap(rig, monkeypatch):
    hook()
    card = show(rig)
    now = time.time()
    monkeypatch.setattr(asks.time, "time", lambda: now + asks.MAX_AGE + 1)
    press(rig, tap(card))
    assert rig.scr.sent == []
    assert only_utility_buttons(show(rig))


def test_changed_stamp_cannot_rebind_or_accept_an_old_tap(rig):
    hook()
    card = show(rig)
    before = D.parse(rig.scr.body)
    changed = screen().replace("❯ 1.", "  1.").replace("  2.", "❯ 2.")
    after = D.parse(changed)
    assert after.identity == before.identity and after.stamp != before.stamp
    rig.scr.body = changed
    press(rig, tap(card))
    assert rig.scr.sent == []
    assert only_utility_buttons(show(rig, changed))


@pytest.mark.parametrize("alter", [
    lambda s: s.replace(QUESTION, "Do not " + QUESTION),
    lambda s: s.replace("?", "!"),
    lambda s: s.replace(LABELS[0], LABELS[0] + " later"),
    lambda s: s.replace(LABELS[0], LABELS[1]).replace("2. " + LABELS[1], "2. " + LABELS[0]),
])
def test_question_and_complete_ordered_labels_must_match(rig, alter):
    hook()
    assert only_utility_buttons(show(rig, alter(screen())))


def test_text_elsewhere_on_screen_is_not_the_question(rig):
    hook()
    assert only_utility_buttons(show(rig, QUESTION + "\n\n\n" + screen("A different question?")))


def test_labels_with_same_display_prefix_do_not_match(rig):
    prefix = "A" * 80
    hook(labels=[prefix + " first", "Second"])
    assert only_utility_buttons(show(rig, screen(labels=[prefix + " changed", "Second"])))


def test_other_session_record_is_not_proof(rig):
    hook(session="other")
    assert only_utility_buttons(show(rig))


@pytest.mark.parametrize("attacker", ["non-owner", "another-topic", "another-session", "another-chat", "bot"])
def test_owner_and_exact_session_topic_required_even_at_handler(rig, attacker):
    hook()
    card = show(rig)
    mid = D.rec_get("s")["message_id"]
    m = {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": rig.thread, "message_id": mid}
    q = {"from": {"id": OWNER}, "data": tap(card), "message": m}
    if attacker == "non-owner":
        q["from"]["id"] = OWNER + 1
    elif attacker == "another-topic":
        m["message_thread_id"] += 1
    elif attacker == "another-chat":
        m["chat"]["id"] += 1
    elif attacker == "bot":
        q["from"]["is_bot"] = True
    else:
        m["message_thread_id"] = rig.topics.ensure("other", "other task")
    assert D.button(q, rig.cfg, rig.bot, m, "s") == "ignored"
    assert rig.scr.sent == []
    press(rig, tap(card))  # invalid attempts did not consume the legitimate owner's proof
    assert rig.scr.sent == ["1"]


def test_public_callback_router_rejects_nonowner_and_wrong_topic(rig):
    hook()
    card = show(rig)
    assert press(rig, tap(card), user=OWNER + 1) == "ignored"
    assert press(rig, tap(card), thread=999) == "ignored"
    assert rig.scr.sent == []


def test_double_tap_answers_once_even_if_the_screen_does_not_move(rig):
    hook()
    card = show(rig)
    press(rig, tap(card))
    press(rig, tap(card))
    assert rig.scr.sent == ["1"]
    assert only_utility_buttons(show(rig))


def test_concurrent_claims_consume_proof_once(rig):
    hook()
    show(rig)
    d = D.parse(rig.scr.body)
    proof = D.proof_for("s", d)
    with ThreadPoolExecutor(max_workers=4) as pool:
        accepted = list(pool.map(lambda _: asks.claim("s", proof, d.identity, d.stamp), range(8)))
    assert accepted.count(True) == 1


def test_old_card_cannot_answer_a_repeated_identical_question(rig):
    hook()
    card = show(rig)
    old_mid = D.rec_get("s")["message_id"]
    hook()
    show(rig)
    press(rig, tap(card), message_id=old_mid)
    assert rig.scr.sent == []
    press(rig, tap(last_card(rig)))
    assert rig.scr.sent == ["1"]


def test_new_hook_record_cannot_authorize_an_earlier_card(rig):
    hook()
    card = show(rig)
    hook()  # same text, but a different invocation: no card for it yet
    press(rig, tap(card))
    assert rig.scr.sent == []


@pytest.mark.parametrize("extra", [{"hook_event_name": "PostToolUse"}, {"agent_id": "child"},
                                   {"tool_name": "Bash"}])
def test_only_main_session_pretool_ask_records_proof(rig, extra):
    hook(**extra)
    assert asks.load("s") is None
    assert only_utility_buttons(show(rig))


@pytest.mark.parametrize("at", [float("nan"), float("inf"), True, "now"])
def test_invalid_timestamps_fail_closed(rig, at):
    hook()
    rec = asks.load("s")
    rec["at"] = at
    asks._path("s").write_text(json.dumps(rec))
    assert only_utility_buttons(show(rig))


def test_future_timestamp_is_not_fresh(rig):
    asks.record("s", tool_input(), now=time.time() + 60)
    assert only_utility_buttons(show(rig))


def test_invalid_replacement_clears_earlier_proof(rig):
    hook()
    assert asks.load("s")
    H.hook_ask(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                           "session_id": "s", "tool_input": {}}))
    assert asks.load("s") is None
    assert only_utility_buttons(show(rig))


def test_hook_records_complete_hash_and_installer_registers_synchronous_matcher(rig, capsys):
    hook()
    rec = asks.load("s")
    assert rec["session"] == "s"
    assert rec["questions"][0]["hash"] == asks.fingerprint(QUESTION, LABELS, False)
    assert asks.MAX_AGE <= 1800
    installed, _ = install.merge_settings({})
    entry = installed["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "AskUserQuestion"
    command = entry["hooks"][0]
    assert install.is_hook_command(command["command"], "hook-ask") and not command.get("async")
    assert install.merge_settings(installed)[0] == installed
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("question", [
    "Buy the domain for 2 years?", "Approve the transfer of 300 shekels?", "Send the reminder to all 40 parents?",
    "Merge PR 12 into main now?", "Push to main?", "Run the migration on the live database?",
    "Post it to the channel?", "לשלוח את ההודעה לכל ההורים?", "לקנות את הדומיין?"])
def test_money_messaging_and_repository_actions_stay_typed_only(rig, question):
    assert hook(question=question) == 0
    assert only_utility_buttons(show(rig, screen(question=question)))
