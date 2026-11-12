"""A running /compact is never a question (live bug 2026-11-02, session 41678ab8).

When the bridge auto-compacted, the topic got "there is an open question in the session, its card is
arriving" and then an "unknown screen, keys only" card. Nothing was open: the screen was the compaction
spinner under the previous reply (numbered lists, and the spinner's own "esc to interrupt" row, which reads
like a key hint). Rule under test: while a session compacts, no dialog notice or card is posted for it; a
real dialog (permission prompt, AskUserQuestion, trust, resume) still is.
"""
import json
import os
import time

from ctb import compact as C
from ctb import dialogs as D
from ctb import hooks as H
from ctb import inbox as I
from ctb import topics as T
from test_dialogs import PERMISSION, SINGLE, rig  # noqa: F401  (fixtures)

REPLY = """\
● Here are the options I see:

  1. Keep the current layout
  2. Move the sidebar to the right
  3. Drop the footer entirely

  Which one do you prefer?
"""
SPINNER = "✻ Compacting conversation… (23s · ↓ 1.2k tokens · esc to interrupt)"
RULE = "─" * 40
# The spinner under the reply, then an "esc to interrupt" row of its own (a key-hint look-alike).
COMPACTING_HINT = REPLY + f"\n{SPINNER}\n  esc to interrupt\n"
# The spinner with a selector-looking row of the reply above it.
COMPACTING_SELECTOR = REPLY.replace("  1. Keep", "❯ 1. Keep") + f"\n{SPINNER}\n  ⎿  Tip: use /clear to start fresh\n"
# Same reply and hint row, no spinner text on the screen: only the bridge knows it compacts.
HINT_ONLY = REPLY + "\n  esc to interrupt\n"


def busy(session="s"):
    T.session_state_path(session).parent.mkdir(parents=True, exist_ok=True)
    T.session_state_path(session).write_text(json.dumps({"state": "busy"}))


def notices(rig):
    return [p.get("text", "") for p in rig.bot.methods("sendMessage")]


def test_the_spinner_screen_is_not_a_dialog():
    for body in (COMPACTING_HINT, COMPACTING_SELECTOR, REPLY + f"\n{SPINNER}\n\n{RULE}\n❯ \n{RULE}\n"):
        assert D.compacting_screen(body)
        assert not I._dialog_open(body)
    # the look-alike alone (no spinner) is what the old checks took for a dialog
    assert D.bottom_hint(HINT_ONLY) and I._dialog_open(HINT_ONLY)


def test_a_spinner_far_up_in_the_history_does_not_hide_a_real_dialog():
    old = "\n".join([SPINNER] + [f"  filler {i}" for i in range(20)])
    assert not D.compacting_screen(old + "\n" + PERMISSION)
    assert I._dialog_open(old + "\n" + PERMISSION)
    assert not D.compacting_screen("● Compacting is the next step for this plan.\n  1. yes\n  2. no\n\n❯ ")


def test_cli_command_during_compaction_posts_no_question_notice_or_card(rig):
    rig.scr.body = COMPACTING_HINT
    I.cli_command(rig.cfg, rig.bot, {"message_thread_id": rig.thread, "chat": {"id": 7}}, "s", "/context")
    assert not any("open in the session" in x or "unknown" in x.lower() for x in notices(rig))
    assert D.rec_get("s") == {}
    assert not D.send_card(rig.cfg, rig.bot, "s", rig.thread)


def test_the_bridge_marker_alone_keeps_a_hint_look_alike_quiet(rig):
    """PreCompact fired (or the bridge typed /compact) and the spinner text is not on the pane yet."""
    rig.scr.body = HINT_ONLY
    assert D.open_screen("s")[3]  # without a compaction note it is taken for a dialog
    C.note_compacting("s")
    assert not D.open_screen("s")[3]
    assert not I._dialog_open(HINT_ONLY, "s")
    assert not D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    I.cli_command(rig.cfg, rig.bot, {"message_thread_id": rig.thread, "chat": {"id": 7}}, "s", "/context")
    assert not any("open in the session" in x for x in notices(rig))
    rig.bot.calls.clear()
    busy()
    D._first_seen.clear()
    assert D.remind(rig.cfg, rig.bot, rig.topics, now=1000.0) == 0
    assert D.remind(rig.cfg, rig.bot, rig.topics, now=1030.0) == 0
    assert notices(rig) == []


def test_the_watcher_stays_quiet_on_the_spinner_screen(rig):
    rig.scr.body = COMPACTING_SELECTOR
    busy()
    D._first_seen.clear()
    assert D.remind(rig.cfg, rig.bot, rig.topics, now=1000.0) == 0
    assert D.remind(rig.cfg, rig.bot, rig.topics, now=1030.0) == 0
    assert rig.bot.methods("sendMessage") == []


def test_a_real_dialog_after_the_compaction_still_gets_its_card(rig):
    C.note_compacting("s")
    rig.scr.body = COMPACTING_HINT
    assert not D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    # the compaction ended (SessionStart): the hint look-alike is a dialog again, and so is a real prompt
    C.mark_done("s")
    assert not C.in_progress("s")
    rig.scr.body = SINGLE
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert D.rec_get("s").get("message_id") and not D.rec_get("s").get("unparsed")


def test_a_parsed_dialog_shows_even_if_the_compaction_note_is_stale(rig):
    """A failed compaction never sends SessionStart: a real prompt must not stay hidden behind its note."""
    C.note_compacting("s")
    rig.scr.body = PERMISSION
    assert D.open_screen("s")[2] is not None and D.send_card(rig.cfg, rig.bot, "s", rig.thread)


def test_the_note_expires_and_session_start_clears_it():
    C.note_compacting("s")
    assert C.in_progress("s")
    assert not C.in_progress("s", now=time.time() + C.PRE_TTL + 1)
    assert C.mark_done("s") is False  # no progress message was running
    assert not C.in_progress("s")


def test_the_precompact_hook_notes_the_compaction(monkeypatch):
    from ctb.config import Config
    monkeypatch.setattr(H, "load_config", lambda: Config(bot_token="123456:" + "A" * 35, owner_chat_id=7))
    spawned = []
    monkeypatch.setattr(H, "spawn", lambda cmd, **kw: spawned.append(cmd))
    assert H.hook_lifecycle(json.dumps({"session_id": "s", "hook_event_name": "PreCompact"})) == 0
    assert C.in_progress("s")


def test_progress_start_notes_the_compaction_before_the_thread_runs(rig, monkeypatch):
    started = []
    monkeypatch.setattr(C, "run", lambda *a, **k: started.append(a))
    monkeypatch.setattr(I.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda self: None})())
    I._start_compact_progress(rig.cfg, rig.bot, rig.thread, "s", "%1")
    assert C.in_progress("s")


def test_auto_compact_does_not_start_a_second_compaction(rig, monkeypatch):
    rig.scr.body = REPLY + "\n❯ \n"
    C.note_compacting("s")
    monkeypatch.setattr(I, "session_busy", lambda s: False)
    typed = []
    monkeypatch.setattr(I, "cli_command", lambda *a, **k: typed.append(a))
    assert I.auto_compact("s", 300000, cfg=rig.cfg, bot=rig.bot, topics=rig.topics, sleep_fn=lambda s: None) is False
    assert typed == []


# --- real prompts that merely say "Compacting..." are dialogs, with or without a compaction note -------------
RULE_ROW = "─" * 60
DASH = "╌" * 60


def perm(desc, cmd, opts=("Yes", "Yes, and don't ask again", "No")):
    rows = "\n".join((" ❯ " if i == 0 else "   ") + f"{i + 1}. {x}" for i, x in enumerate(opts))
    return (f"⏺ Working\n\n{RULE_ROW}\n Bash command\n {desc}\n{DASH}\n {cmd}\n{DASH}\n Do you want to proceed?\n{rows}\n\n"
            " Esc to cancel · Tab to amend\n")


LOOKALIKES = {
    "description": perm("Compacting the database...", "sqlite3 app.db 'VACUUM'"),
    "last command row": perm("Rotate logs", "gzip -9 old.log\n # Compacting logs..."),
    "option description": f"{RULE_ROW}\n ☐ Context\n\nThe context is nearly full. What now?\n\n❯ 1. Compact now\n"
                          "     Compacting… frees context but loses detail\n  2. Keep going\n     Continue without compaction\n"
                          "  3. Type something.\n\nEnter to select · ↑/↓ to navigate · Esc to cancel\n",
}


import pytest  # noqa: E402


@pytest.mark.parametrize("name", list(LOOKALIKES))
@pytest.mark.parametrize("noted", [False, True])
def test_a_prompt_that_says_compacting_is_still_a_dialog(rig, name, noted):
    body = LOOKALIKES[name]
    assert D.parse(body) is not None and not D.compacting_screen(body)
    if noted:
        C.note_compacting("s")
    rig.scr.body = body
    assert D.open_screen("s")[3] and I._dialog_open(body, "s") and I._dialog_open(body)
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert not D.rec_get("s").get("unparsed")


def test_the_spinner_pattern_is_the_spinner_only():
    for ok in (SPINNER, "· Compacting conversation… (5s)", "✢ Compacting conversation...", "* Compacting conversation…"):
        assert D.compacting_screen(ok), ok
    for no in ("  Compacting the database...", "# Compacting logs...", "     Compacting… frees context",
               "● Compacting conversation… was the plan"):
        assert not D.compacting_screen(no), no


def test_a_dialog_on_the_spinner_screen_is_still_open(rig):
    """A real prompt drawn together with a spinner row (a tool asks while the compaction text lingers)."""
    body = SPINNER + "\n" + PERMISSION
    rig.scr.body = body
    C.note_compacting("s")
    assert D.parse(body) is not None and D.open_screen("s")[3] and I._dialog_open(body, "s")


def test_the_strict_rule_lasts_only_the_first_moments(rig):
    """Esc / a failed compaction sends no SessionStart: after the window the screen decides alone."""
    rig.scr.body = HINT_ONLY
    C.note_compacting("s")
    assert C.just_started("s") and not D.open_screen("s")[3]
    note = C.precompact_marker("s")
    old = time.time() - C.STRICT_WINDOW - 5
    os.utime(note, (old, old))
    assert C.in_progress("s") and not C.just_started("s")  # still "compacting" for auto_compact, not for dialogs
    assert D.open_screen("s")[3] and I._dialog_open(HINT_ONLY, "s")
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    # while the spinner is on the pane the same old note still keeps the screen quiet
    rig.scr.body = COMPACTING_HINT
    assert not D.open_screen("s")[3]


def test_a_finished_progress_marker_does_not_count(rig):
    C.private_dir(C.directory())
    C.write_private(C.marker("s"), str(time.time()))
    assert C.in_progress("s") and C.just_started("s")
    C.write_private(C.done_marker("s"), "")
    assert not C.in_progress("s") and not C.just_started("s")
    C.done_marker("s").unlink()
    old = time.time() - C.PRE_TTL - 1
    os.utime(C.marker("s"), (old, old))
    assert not C.in_progress("s")


TIP = """  Summary: The voice for both lessons is recording now.
✻ Cogitated for 2m 5s · done 11:08 PM · 5 shells still running
✦ Heads up · the script stage for tomorrow will probably never start,
  because its wait command waits on itself.
  1: Learn more   2: Knew this already   0: Dismiss
────────────────────────────────────────────────────────────────────────────────
❯ 
────────────────────────────────────────────────────────────────────────────────
  Opus 5.5 · week 92%
  ⏵⏵ bypass permissions on · 5 shells
"""


def test_claude_codes_tip_box_above_the_idle_prompt_is_not_a_question():
    assert I._DIALOG_HINT.search(TIP)  # the tip's option row alone reads like a menu
    assert not I._dialog_open(TIP) and not I._dialog_open(TIP, "s")
    assert I._dialog_open(TIP + "\n" + PERMISSION)  # a real question below it still counts


HEBREW_TIP = """\u200f   למשתמשים עם גרסת ה-App Store הבאה, אחרי הכן שלך.

✻ Baked for 10s · done 11:29 PM · 1 monitor still running
                                                                          [-]
\u200f✦ Heads up · השחרור שאישרת מעלה את גרסת הסכמת השימוש בכלי ה־AI של המנהלים
\u200fלגרסה 5, ולכן ייתכן שהחיבורים של המנהלים ל־AI יעצרו עד שיאשרו מחדש.
1: Learn more   2: Knew this already   0: Dismiss
"""


def test_hebrew_tip_box_marked_rtl_is_not_a_question():
    # Regression: a Hebrew tip, its rows marked U+200F by pane_text, blocked /compact
    assert not I._dialog_open(HEBREW_TIP) and not I._dialog_open(HEBREW_TIP, "s")
    assert I._dialog_open(HEBREW_TIP + "\n" + PERMISSION)
