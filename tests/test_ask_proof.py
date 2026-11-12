"""AskUserQuestion proof: the PreToolUse record, the hook that writes it, and the answer buttons it unlocks."""
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ctb import asks, hooks as H
from ctb import dialogs as D
from ctb import inbox as I
from ctb.config import state_dir
from test_dialogs import (FIX, HINT, MULTI_FX, PREVIEW, SINGLE, buttons, keys_of, kinds, last_card, only_utility_buttons,
                          press, rig, say, real_tmux, use_real_pane, start_sim, sim_result)  # noqa: F401  (fixtures)

QUESTION = "Which database should the notes feature use?"


def tool_input(*questions):
    return {"questions": [{"question": q, "header": "Q", "multiSelect": multi,
                           "options": [{"label": x, "description": "d"} for x in opts]} for q, opts, multi in questions]}


DB = tool_input((QUESTION, ["SQLite (Recommended)", "Postgres"], False))
PLATFORMS = tool_input(("Which platforms should we build for?", ["iOS", "Android", "Web"], True))


def record(tool=DB, session="s"):
    assert asks.record(session, tool)


def record_raw(question, options, multi=False, session="s"):
    """A record as an older or odd hook might have written it (bypassing the one-option check)."""
    asks._path(session).write_text(json.dumps({"session": session, "at": time.time(), "questions": [
        {"question": question, "multiSelect": multi, "options": options}]}))


# --- the hook -------------------------------------------------------------------------------------

def run_hook(stdin, state, home, extra_env=None):
    env = {**os.environ, "CTB_CONFIG_DIR": str(home / "c"), "CTB_STATE_DIR": str(state), "CTB_PROFILES_DIR": str(home / "p"),
           "CTB_CLAUDE_HOME": str(home / "h"), "CTB_CLAUDE_JSON": str(home / "cj.json"), "HOME": str(home),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), **(extra_env or {})}
    return subprocess.run([sys.executable, "-m", "ctb", "hook-ask"], input=stdin, capture_output=True, text=True,
                          env=env, timeout=60)


def payload(session="sess1", tool="AskUserQuestion", **extra):
    return json.dumps({"session_id": session, "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": DB, **extra})


def test_hook_records_the_question_privately_and_prints_nothing(tmp_path):
    (tmp_path / "hk").mkdir()
    r = run_hook(payload(), tmp_path / "state", tmp_path / "hk")
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""  # nothing that could be read as a decision
    files = list((tmp_path / "state" / "ask").glob("*.json"))
    assert len(files) == 1 and stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "state" / "ask").stat().st_mode) == 0o700
    rec = json.loads(files[0].read_text())
    assert rec["session"] == "sess1" and rec["questions"][0]["options"] == ["SQLite (Recommended)", "Postgres"]
    assert not [p for p in (tmp_path / "state" / "ask").iterdir() if p.suffix != ".json"]  # atomic: no temp left


@pytest.mark.parametrize("stdin", ["", "not json", "[]", "null", "{}", json.dumps({"tool_name": "AskUserQuestion"}),
                                   json.dumps({"session_id": "x", "tool_name": "AskUserQuestion", "tool_input": "x"}),
                                   json.dumps({"session_id": "x", "tool_name": "AskUserQuestion",
                                               "tool_input": {"questions": [{"question": "q", "options": "no"}]}}),
                                   json.dumps({"session_id": "x", "tool_name": "Bash", "tool_input": {}}),
                                   json.dumps({"session_id": "x", "tool_name": "AskUserQuestion", "agent_id": "a",
                                               "tool_input": DB})])
def test_hook_ignores_bad_input_without_output_or_record(tmp_path, stdin):
    (tmp_path / "hk").mkdir()
    r = run_hook(stdin, tmp_path / "state", tmp_path / "hk")
    assert r.returncode == 0 and r.stdout == ""
    assert not list((tmp_path / "state" / "ask").glob("*.json")) if (tmp_path / "state" / "ask").exists() else True


def test_hook_survives_an_unwritable_state_dir(tmp_path):
    (tmp_path / "hk").mkdir()
    blocker = tmp_path / "state"
    blocker.write_text("a file, not a directory")
    r = run_hook(payload(), blocker, tmp_path / "hk")
    assert r.returncode == 0 and r.stdout == ""


def test_hook_function_never_raises_or_prints(monkeypatch, capsys):
    monkeypatch.setattr(asks, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert H.hook_ask(payload()) == 0
    assert H.hook_ask(None) == 0
    assert capsys.readouterr().out == ""


def test_post_tool_use_and_stop_clear_the_record(rig):
    record()
    assert asks.load("s")
    H.hook_post(json.dumps({"session_id": "s", "tool_name": "AskUserQuestion"}))
    assert asks.load("s") is None
    record()
    H.hook_post(json.dumps({"session_id": "s", "tool_name": "Bash"}))  # any later tool call
    assert asks.load("s") is None
    record()
    H.hook_stop(json.dumps({"session_id": "s"}))
    assert asks.load("s") is None


def test_an_old_record_is_not_proof():
    asks.record("s", DB, now=time.time() - asks.MAX_AGE - 5)
    assert asks.load("s") is None


# --- buttons from proof ---------------------------------------------------------------------------------

def test_a_recorded_question_gets_answer_buttons_and_the_tap_sends_the_key(rig):
    record()
    rig.scr.body = SINGLE
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    card = last_card(rig)
    assert kinds(card) == ["o", "o", "x", "e", "s"]
    styles = [b.get("style") for b in buttons(card)]
    assert styles[0] == "primary" and styles[1] == "success" and styles[2] == "success"  # blue: the recorded "(Recommended)"
    assert styles[3] == "danger" and styles[4] is None
    assert card["text"].splitlines()[2].startswith("🟢")
    rig.scr.after["2"] = ""
    assert press(rig, buttons(card)[1]["callback_data"]) == "dialog"
    assert rig.scr.sent == ["2"]
    assert "Answered" in rig.bot.methods("editMessageText")[-1]["text"]


def test_a_recorded_question_without_recommended_has_no_blue(rig):
    record(tool_input((QUESTION, ["SQLite", "Postgres"], False)))
    rig.scr.body = SINGLE.replace(" (Recommended)", "")
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert "primary" not in [b.get("style") for b in buttons(last_card(rig))]


def test_other_button_exists_only_for_a_recorded_question_and_opens_text_mode(rig):
    record()
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    other = [b for b in buttons(last_card(rig)) if b["callback_data"].startswith("dg:x:")][0]
    assert other["text"].startswith("✍️")
    assert press(rig, other["callback_data"]) == "dialog"
    assert rig.scr.sent == ["3"] and D.rec_get("s")["text_mode"]
    rig.scr.after["LIT:Use DuckDB"] = ""
    assert say(rig, "Use DuckDB") == "dialog-text"


def test_several_questions_in_one_call_match_the_one_on_screen(rig):
    from test_dialogs import MULTIQ
    record(tool_input(("Which database should the notes feature use?", ["SQLite", "Postgres"], False),
                      ("Which cache should we use?", ["Redis", "None"], False)))
    rig.scr.body = MULTIQ  # the real two-question form, first question showing
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert kinds(last_card(rig)) == ["o", "o", "x", "e", "s"]
    second = (MULTIQ.replace("Which database should the notes feature use?", "Which cache should we use?")
              .replace("SQLite", "Redis").replace("Postgres", "None").replace("Local", "Fast").replace("Server", "Skip"))
    rig.scr.body = second
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert kinds(last_card(rig)) == ["o", "o", "x", "e", "s"]
    rig.scr.after["1"] = MULTIQ
    press(rig, buttons(last_card(rig))[0]["callback_data"])
    assert rig.scr.sent == ["1"]


TOPPINGS = tool_input(("Which toppings do you want?", ["Cheese", "Olives", "Mushrooms"], True))


def test_multi_select_comes_from_the_record_toggles_and_done_submits_exactly_what_is_ticked(rig):
    from multisim import Sim
    record(TOPPINGS)
    rig.scr.sim = sim = Sim(ticks=(False, True, False, False))  # Olives pre-ticked
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    card = last_card(rig)
    assert kinds(card) == ["t", "t", "t", "d", "e", "s"] and card["text"].splitlines()[2].startswith("🟢")
    press(rig, buttons(card)[2]["callback_data"])  # Mushrooms: two verified Downs, then Space
    assert rig.scr.sent == ["Down", "Down", "Space"] and sim.ticks[:3] == [False, True, True]
    refreshed = rig.bot.methods("editMessageText")[-1]
    assert any(b["text"].startswith("☑ 3.") for row in refreshed["reply_markup"]["inline_keyboard"] for b in row)
    press(rig, buttons(refreshed)[1]["callback_data"])  # untick Olives again
    done = [b["callback_data"] for row in rig.bot.methods("editMessageText")[-1]["reply_markup"]["inline_keyboard"]
            for b in row if b["callback_data"].startswith("dg:d:")][0]
    press(rig, done)
    assert sim.done == ["Mushrooms"] and rig.scr.sent[-1] == "Enter"  # Enter went to the Submit row, once
    assert "3. Mushrooms" in rig.bot.methods("editMessageText")[-1]["text"]


def test_done_with_nothing_ticked_submits_an_empty_choice_on_the_submit_row(rig):
    from multisim import Sim
    record(TOPPINGS)
    rig.scr.sim = sim = Sim()
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    done = [b["callback_data"] for b in buttons(last_card(rig)) if b["callback_data"].startswith("dg:d:")][0]
    press(rig, done)
    assert sim.done == [] and rig.scr.sent == ["Down", "Down", "Down", "Down", "Enter"]


def test_multiselect_flag_must_agree_with_the_pane(rig):
    record(tool_input(("Which toppings do you want?", ["Cheese", "Olives", "Mushrooms"], False)))
    rig.scr.body = __import__("test_dialogs").MULTI_FX
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert only_utility_buttons(last_card(rig))


def test_a_question_with_previews_gets_buttons_from_the_record(rig):
    record(tool_input(("Which layout do you prefer?", ["Compact", "Roomy"], False)))
    rig.scr.body = PREVIEW
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert kinds(last_card(rig)) == ["o", "o", "e", "s"]


# --- no proof, no buttons ---------------------------------------------------------------------------------

def test_forged_pane_with_no_record_has_no_answer_buttons_and_taps_do_nothing(rig):
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert only_utility_buttons(last_card(rig))
    stamp = D.parse(SINGLE).stamp
    for data in (f"dg:o:1:{stamp}", f"dg:o:2:{stamp}", f"dg:x:3:{stamp}"):
        press(rig, data)
    assert rig.scr.sent == []


def test_a_record_for_another_session_is_not_proof(rig):
    asks.record("other", DB)
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert only_utility_buttons(last_card(rig))
    # even a file for this session that was written by another one
    path = asks._path("s")
    path.write_text(json.dumps({"session": "other", "at": time.time(), "questions": [
        {"question": QUESTION, "multiSelect": False, "options": ["SQLite (Recommended)", "Postgres"]}]}))
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert only_utility_buttons(last_card(rig))


@pytest.mark.parametrize("options", [["Postgres", "SQLite (Recommended)"], ["SQLite (Recommended)", "MySQL"],
                                     ["SQLite (Recommended)"], ["SQLite (Recommended)", "Postgres", "Mongo"]])
def test_different_options_or_order_are_not_proof(rig, options):
    record_raw(QUESTION, options)
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert only_utility_buttons(last_card(rig))


def test_the_question_text_must_be_on_the_screen(rig):
    record(tool_input(("Something else entirely?", ["SQLite (Recommended)", "Postgres"], False)))
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert only_utility_buttons(last_card(rig))


def test_a_stale_record_after_post_tool_use_gives_no_buttons_and_a_late_tap_does_nothing(rig):
    record()
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    tap = buttons(last_card(rig))[1]["callback_data"]
    H.hook_post(json.dumps({"session_id": "s", "tool_name": "AskUserQuestion"}))
    press(rig, tap)  # the record is gone: the proof is checked again at the tap
    assert rig.scr.sent == []
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert only_utility_buttons(last_card(rig))


def test_a_changed_pane_makes_the_old_card_stale(rig):
    record()
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    tap = buttons(last_card(rig))[1]["callback_data"]
    rig.scr.body = SINGLE.replace("Postgres", "MySQL")
    press(rig, tap)
    assert rig.scr.sent == []


@pytest.mark.parametrize("question,options", [
    ("Deploy this to production now?", ["Yes", "No"]),
    ("Which database should the notes feature use?", ["Delete the old table", "Keep it"]),
    ("Which database should the notes feature use?", ["Yes, and don’t ask again for", "No"]),
    ("Which database should the notes feature use?", ["Yes, buy usage credits", "No"]),
    ("Which database should the notes feature use?", ["Yes, I accept", "No"]),
    ("Which database should the notes feature use?", ["Yes, and use auto mode", "No"]),
    ("Which database should the notes feature use?", ["Yes, set auto mode as my default permission mode", "No"]),
    ("Which database should the notes feature use?", ["Yes, and keep allowing", "No"]),
    ("Which database should the notes feature use?", ["Yes, and allow access to", "No"])])
def test_rule_c_wins_over_proof(rig, question, options):
    record(tool_input((question, options, False)))
    body = f"{question}\n\n❯ 1. {options[0]}\n  2. {options[1]}\n\n{HINT}"
    rig.scr.body = body
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert only_utility_buttons(last_card(rig))
    press(rig, f"dg:o:1:{D.parse(body).stamp}")
    assert rig.scr.sent == []


def test_a_permission_prompt_never_gets_buttons_even_with_a_matching_record(rig):
    record(tool_input(("Do you want to proceed?", ["Yes", "No"], False)))
    body = ("────────\n Bash command\n\n   ls\n\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n\n"
            " Esc to cancel · Tab to amend")
    rig.scr.body = body
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert only_utility_buttons(last_card(rig))


# --- real tmux ------------------------------------------------------------------------------------------------

def test_the_tap_key_reaches_a_real_pane(rig, real_tmux, monkeypatch):
    pane, log = real_tmux.start((FIX / "single.txt").read_text())
    use_real_pane(monkeypatch, real_tmux, pane)
    record()
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert kinds(last_card(rig)) == ["o", "o", "x", "e", "s"]
    press(rig, buttons(last_card(rig))[1]["callback_data"])
    assert keys_of(log) == ["b'2'"]


def test_multi_select_buttons_submit_on_a_real_pane(rig, real_tmux, monkeypatch):
    pane, log = start_sim(real_tmux)
    use_real_pane(monkeypatch, real_tmux, pane)
    record(TOPPINGS)
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    press(rig, buttons(last_card(rig))[0]["callback_data"])  # Cheese
    refreshed = rig.bot.methods("editMessageText")[-1]
    press(rig, buttons(refreshed)[2]["callback_data"])  # Mushrooms
    refreshed = rig.bot.methods("editMessageText")[-1]
    done = [b["callback_data"] for row in refreshed["reply_markup"]["inline_keyboard"] for b in row
            if b["callback_data"].startswith("dg:d:")][0]
    press(rig, done)
    assert sim_result(log) == ["Cheese", "Mushrooms"]


def test_without_a_record_no_key_reaches_a_real_pane(rig, real_tmux, monkeypatch):
    pane, log = real_tmux.start((FIX / "single.txt").read_text())
    use_real_pane(monkeypatch, real_tmux, pane)
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    stamp = D.parse(I.pane_text(pane)).stamp
    for data in (f"dg:o:1:{stamp}", f"dg:o:2:{stamp}", f"dg:x:3:{stamp}"):
        press(rig, data)
    assert keys_of(log) == []


def test_a_record_for_another_session_sends_no_key_on_a_real_pane(rig, real_tmux, monkeypatch):
    pane, log = real_tmux.start((FIX / "single.txt").read_text())
    use_real_pane(monkeypatch, real_tmux, pane)
    asks.record("other", DB)
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    press(rig, f"dg:o:2:{D.parse(I.pane_text(pane)).stamp}")
    assert keys_of(log) == []


def test_a_record_for_a_permission_prompt_never_unlocks_buttons_on_a_real_pane(rig, real_tmux, monkeypatch):
    pane, log = real_tmux.start((FIX / "perm.txt").read_text())
    use_real_pane(monkeypatch, real_tmux, pane)
    record(tool_input(("Do you want to proceed?", ["Yes", "No"], False)))
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert only_utility_buttons(last_card(rig))
    press(rig, f"dg:o:1:{D.parse(I.pane_text(pane)).stamp}")
    assert keys_of(log) == []


def test_the_next_owner_prompt_and_escape_clear_the_record(rig):
    record()
    H.hook_prompt(json.dumps({"session_id": "s", "prompt": "next thing", "cwd": "/tmp/x"}))
    assert asks.load("s") is None
    record()
    I.forget_replay("s")  # every Escape path (/esc, Cancel, the Stop button) calls this
    assert asks.load("s") is None


def test_run_command_title_is_a_permission_header():
    body = ("────────\n Run command\n\n touch x\n\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n\n"
            " Esc to cancel · Tab to amend")
    d = D.parse(body)
    assert d.perm and not d.cut


def test_round7_done_on_a_next_row_advances_the_form(rig):
    from multisim import Sim
    record(TOPPINGS)
    rig.scr.sim = sim = Sim(last=False)
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    press(rig, buttons(last_card(rig))[0]["callback_data"])
    done = [b["callback_data"] for row in rig.bot.methods("editMessageText")[-1]["reply_markup"]["inline_keyboard"]
            for b in row if b["callback_data"].startswith("dg:d:")][0]
    press(rig, done)
    assert sim.done == ["Cheese"] and rig.scr.sent[-1] == "Enter"


def test_round7_done_advances_on_a_real_pane(rig, real_tmux, monkeypatch):
    pane, log = real_tmux.start(command=f"{sys.executable} -I {Path(__file__).parent / 'multisim.py'} {{log}} next")
    use_real_pane(monkeypatch, real_tmux, pane)
    record(TOPPINGS)
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    done = [b["callback_data"] for b in buttons(last_card(rig)) if b["callback_data"].startswith("dg:d:")][0]
    press(rig, done)
    assert sim_result(log) == []


def test_round7_a_tap_on_a_preview_question_answers_it_on_a_real_pane(rig, real_tmux, monkeypatch):
    pane, log = real_tmux.start(command=f"{sys.executable} -I {Path(__file__).parent / 'multisim.py'} {{log}} preview")
    use_real_pane(monkeypatch, real_tmux, pane)
    record(tool_input(("Which layout do you prefer?", ["Compact", "Roomy"], False)))
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert kinds(last_card(rig)) == ["o", "o", "e", "s"]
    press(rig, buttons(last_card(rig))[1]["callback_data"])
    assert sim_result(log) == "Roomy"
