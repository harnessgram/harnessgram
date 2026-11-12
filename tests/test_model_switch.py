"""/model reports what Claude Code really did, and every label shows the model the session is on now.

2026-11-10: the Opus button answered "✅ Model: opus" at once while Claude Code asked to confirm the
mid-conversation switch, the question was cancelled and it said "Kept model as `Fable 5.1`". A second
try said "Set model to `Opus 5.5`", yet the status card and the keyboard kept showing Fable 5.1.
"""
import json
from types import SimpleNamespace

import pytest

from conftest import BoxedTmux

pytestmark = pytest.mark.real_type_command  # the real gate (BoxedTmux gives it a box)

from ctb import inbox as I, mirror, models, status, usage
from ctb.config import state_dir
from test_inbox import OWNER, cfg, setup

SID = "a2311d51-0000-4000-8000-000000000001"


def reply(model="claude-fable-5-1"):
    return {"type": "assistant", "message": {"model": model, "role": "assistant", "content": [{"type": "text", "text": "ok"}],
                                             "usage": {"input_tokens": 10, "cache_read_input_tokens": 90_000}}}


def system_out(text):  # Claude Code writes some command answers as system rows ...
    return {"type": "system", "subtype": "local_command", "content": f"<local-command-stdout>{text}</local-command-stdout>"}


def user_out(text):  # ... and others as user rows
    return {"type": "user", "message": {"role": "user", "content": f"<local-command-stdout>{text}</local-command-stdout>"}}


def command(args):
    return {"type": "system", "subtype": "local_command",
            "content": f"<command-name>/model</command-name>\n<command-message>model</command-message>\n<command-args>{args}</command-args>"}


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("")
    monkeypatch.setattr(status, "transcript_of", lambda s: str(path) if s == SID else None)

    def add(*rows):
        with open(path, "a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
    add.path = path
    return add


def saved_models(value):
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / "session-models.json").write_text(json.dumps({SID: value}))


def test_the_model_table_names_and_ids():
    assert models.IDS == {"fable": "claude-fable-5-2", "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
                          "haiku": "claude-haiku-5-5"}
    assert models.LABELS["opus"] == "Opus 5.5" and models.CHOICES == ("fable", "opus", "sonnet", "haiku")
    assert I.MODEL_IDS is models.IDS and I.MODEL_LABELS is models.LABELS
    assert models.display_name("claude-opus-5-5") == "Opus 5.5"
    assert models.display_name("claude-fable-5-1") == "Fable 5.1"  # an id that is not in the table
    assert models.display_name("claude-haiku-4-5-20251001") == "Haiku 4.5"
    assert models.display_name("Opus 5.5") == "Opus 5.5" and models.display_name("") == ""


@pytest.mark.parametrize("text,expected", [
    ("Set model to `Opus 5.5` and saved as your default for new sessions", (True, "Opus 5.5")),
    ("Set model to `Opus 5.5`", (True, "Opus 5.5")),
    ("Set model to `claude-opus-5-5`", (True, "Opus 5.5")),
    ("Set model to claude-opus-5", (True, "Opus 5")),
    ("Kept model as `Fable 5.1`", (False, "Fable 5.1")),
    ("Model 'gpt' not found", None),
    ("", None),
])
def test_claude_code_answers_to_model_are_read(text, expected):
    assert models.parse_result(text) == expected


def test_the_newest_switch_wins_only_until_the_next_reply(transcript):
    transcript(reply())
    assert models.latest_switch(str(transcript.path)) == ""
    transcript(command("opus"), system_out("Kept model as `Fable 5.1`"))
    assert models.latest_switch(str(transcript.path)) == "Fable 5.1"
    transcript(command("opus"), user_out("Set model to `Opus 5.5` and saved as your default for new sessions"))
    assert models.latest_switch(str(transcript.path)) == "Opus 5.5"
    transcript(reply("claude-opus-5-5"))
    assert models.latest_switch(str(transcript.path)) == ""  # the status line knows it by now


def test_card_keyboard_and_footer_show_the_switched_model(transcript):
    """The status line saved Fable before the switch and has not run since."""
    saved_models("Fable 5.1|high")
    transcript(reply(), command("opus"), user_out("Set model to `Opus 5.5`"))
    assert usage.session_model(SID) == ("Opus 5.5", "high")
    assert "🧠 Opus 5.5" in mirror.session_stamp(SID, "he")
    kb = mirror.live_keyboard("", lang="he", session=SID)
    labels = [b["text"] for row in kb["keyboard"] for b in row]
    assert "🧠 Opus 5.5" in labels and not any("Fable" in label for label in labels)
    old = "\U0001F9E0 הקשר 90 אלף\n\U0001F916 ⁨Fable 5.1⁩ · 🎚️ גבוהה"
    assert "⁨Opus 5.5⁩ · 🎚️" in mirror.restamp_footer(old, SID, "he")
    assert mirror.restamp_footer("🧠 90k · Fable 5.1 · high", SID, "en").startswith("🧠 90k · Opus 5.5 · high")


# --- the /model button --------------------------------------------------------------------------------

@pytest.fixture
def rig(tmp_path, monkeypatch, bot, transcript):
    topics, box, thread = setup(tmp_path, bot)
    topics.ensure(SID, "Title")
    typed, screens, answer = [], ["❯ "], {}

    def tmux(*args):
        if args[:1] == ("send-keys",):
            typed.append(args[-1])
            if args[-1] == "Enter" and answer.get("dialog"):
                screens[0] = answer["dialog"]  # Claude Code asks before it switches
            if args[-1] == "Enter" and answer.get("now"):
                transcript(command(typed[-2].split(" ", 1)[1]), answer["now"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(I, "_tmux", BoxedTmux(tmux))
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%5")
    monkeypatch.setattr(I, "pane_text", lambda pane, lines=45: screens[0])
    monkeypatch.setattr(I, "_terminal_background", lambda s, action: action() or True)
    monkeypatch.setattr(I.threading, "Thread", lambda target, args=(), daemon=None: SimpleNamespace(
        start=lambda: target(*args)))
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    backs = []
    monkeypatch.setattr(I, "keyboard_back", lambda thread, **k: backs.append(thread))
    transcript(reply())
    saved_models("Fable 5.1|high")
    m = {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread, "message_id": 20}
    return SimpleNamespace(m=m, typed=typed, screens=screens, answer=answer, backs=backs, bot=bot,
                           transcript=transcript)


def texts(bot):
    return [p["text"] for m, p in bot.calls if m in ("sendMessage", "editMessageText") and "text" in p]


def test_the_button_reports_the_switch_only_after_claude_code_made_it(rig):
    rig.answer["now"] = user_out("Set model to `Opus 5.5` and saved as your default for new sessions")
    I.cli_command(cfg(), rig.bot, rig.m, SID, "/model claude-opus-5-5", menu=True)
    assert rig.typed[-2:] == ["/model claude-opus-5-5", "Enter"]
    assert texts(rig.bot)[-1] == "‏✅ מודל: ⁨Opus 5.5⁩"
    saved = json.loads((state_dir() / "session-models.json").read_text())
    assert saved[SID] == "Opus 5.5|high"          # the card and keyboard show it now, not next turn
    assert rig.backs == [rig.m["message_thread_id"]]


def test_a_cancelled_switch_says_the_model_stayed(rig):
    rig.answer["now"] = system_out("Kept model as `Fable 5.1`")
    I.cli_command(cfg(), rig.bot, rig.m, SID, "/model claude-opus-5-5", menu=True)
    assert texts(rig.bot)[-1] == "‏⚠️ המודל נשאר ⁨Fable 5.1⁩"
    assert not any("✅" in t for t in texts(rig.bot))
    assert rig.backs == []


def test_the_switch_question_is_confirmed_for_the_owner(rig, monkeypatch):
    """The owner's model tap is the decision; Claude Code's "switch mid-conversation?" question is
    answered "yes" by the bridge, with no second card."""
    rig.answer["dialog"] = "Switch model mid-conversation?\n❯ 1. Yes, switch to Opus 5.5\n  2. No, go back\n\nEnter to confirm · Esc to cancel"
    monkeypatch.setattr(I, "pane_runs_session", lambda pane, session, known_pid=None: True)
    monkeypatch.setattr(I, "_pane_from_process", lambda session: "%5")
    cards = []
    monkeypatch.setattr(I.dialogs, "send_card", lambda *a, **k: cards.append(1) or True)
    real = I.dialogs._keys

    def keys(pane, *k):
        if k == ("1",):
            rig.transcript(command("claude-opus-5-5"), user_out("Set model to `Opus 5.5`"))
        return real(pane, *k)

    monkeypatch.setattr(I.dialogs, "_keys", keys)
    I.cli_command(cfg(), rig.bot, rig.m, SID, "/model claude-opus-5-5", menu=True)
    said = texts(rig.bot)
    assert "1" in rig.typed and cards == []
    assert not any("לאשר את ההחלפה" in t for t in said) and said[-1].endswith("⁨Opus 5.5⁩")


def test_a_confirm_question_goes_to_its_card_and_the_result_follows(rig, monkeypatch):
    rig.answer["dialog"] = "Change the model?\n❯ 1. Keep going\n  2. Something else\n\nEnter to confirm · Esc to cancel"
    cards = []

    def send_card(cfg_, bot, session, thread, chat=None, **k):
        cards.append(session)
        rig.transcript(command("claude-opus-5-5"), user_out("Set model to `Opus 5.5`"))  # the owner answered "1"
        return True

    monkeypatch.setattr(I.dialogs, "send_card", send_card)
    I.cli_command(cfg(), rig.bot, rig.m, SID, "/model claude-opus-5-5", menu=True)
    said = texts(rig.bot)
    assert cards == [SID]
    assert any("לאשר את ההחלפה" in t for t in said) and said[-1].endswith("⁨Opus 5.5⁩")


def test_no_answer_is_never_reported_as_done(rig, monkeypatch):
    monkeypatch.setattr(I, "MODEL_WAIT", 0)
    I.cli_command(cfg(), rig.bot, rig.m, SID, "/model claude-opus-5-5", menu=True)
    said = texts(rig.bot)[-1]
    assert "אין תשובה" in said and "⁨Fable 5.1⁩" in said and "✅" not in said


def test_typed_alias_sends_the_exact_id(rig):
    rig.answer["now"] = user_out("Set model to `Sonnet 5.5`")
    I.cli_command(cfg(), rig.bot, rig.m, SID, "/model sonnet")
    assert "/model claude-sonnet-5-5" in rig.typed
    assert texts(rig.bot)[-1].endswith("⁨Sonnet 5.5⁩")


def test_model_buttons_show_exact_names(rig):
    I.cli_command(cfg(), rig.bot, rig.m, SID, "/model")
    kb = rig.bot.methods("sendMessage")[-1]["reply_markup"]["inline_keyboard"]
    labels = [b["text"] for row in kb[:2] for b in row]
    assert labels == ["Fable 5.2", "Opus 5.5", "Sonnet 5.5", "Haiku 5.5"]


# --- review of #138 (finding 4): only the /model command's own answer counts ----------------------------

def effort_answer():
    return [{"type": "system", "subtype": "local_command",
             "content": "<command-name>/effort</command-name>\n<command-message>effort</command-message>\n"
                        "<command-args>high</command-args>"},
            system_out("Set effort level to high")]


def test_another_commands_output_is_not_taken_as_the_model_answer(rig, monkeypatch):
    """An /effort answer lands first; Claude Code's answer to the /model comes on a later look."""
    rig.answer["now"] = None
    looks = []

    def sleep(_):
        looks.append(1)
        if len(looks) == 1:
            rig.transcript(*effort_answer())
        elif len(looks) == 3:
            rig.transcript(command("claude-opus-5-5"), user_out("Set model to `Opus 5.5`"))
    monkeypatch.setattr(I.time, "sleep", sleep)
    I.cli_command(cfg(), rig.bot, rig.m, SID, "/model claude-opus-5-5", menu=True)
    said = texts(rig.bot)
    assert said[-1] == "‏✅ מודל: ⁨Opus 5.5⁩"
    assert not any("effort" in t for t in said)


def test_the_answer_is_matched_to_the_model_that_was_typed(transcript):
    transcript(reply())
    start = transcript.path.stat().st_size
    transcript(command("claude-sonnet-5-5"), user_out("Set model to `Sonnet 5.5`"), *effort_answer())
    assert models.model_answer_after(str(transcript.path), start, "claude-opus-5-5") is None
    transcript(command("claude-opus-5-5"), user_out("Model 'claude-opus-5-5' not available"))
    assert models.model_answer_after(str(transcript.path), start, "claude-opus-5-5") == (
        "out", "Model 'claude-opus-5-5' not available")  # its own failure is its answer
    assert models.model_answer_after(str(transcript.path), start)[1] == "Set model to `Sonnet 5.5`"


def test_an_output_without_a_command_record_counts_only_as_a_model_answer(transcript):
    transcript(system_out("Set effort level to high"))
    assert models.model_answer_after(str(transcript.path), 0, "claude-opus-5-5") is None
    transcript(user_out("Kept model as `Fable 5.1`"))
    assert models.model_answer_after(str(transcript.path), 0, "claude-opus-5-5") == ("out", "Kept model as `Fable 5.1`")


def test_a_session_without_a_status_line_still_shows_its_model_and_thinking_level(transcript):
    """2026-11-11: in some topics the model button showed no model. A session started from the
    app has no terminal status line, so nothing saved its model: the newest reply knows it."""
    row = dict(reply("claude-opus-5-5"), effort="medium")
    agent = dict(reply("claude-haiku-5-5"), effort="low", isSidechain=True)   # a background agent's reply
    broken = {"type": "assistant", "message": {"model": "<synthetic>", "role": "assistant", "content": []}}
    transcript(row, agent, broken)
    assert usage.last_reply_model(str(transcript.path)) == ("Opus 5.5", "medium")
    assert usage.session_model(SID) == ("Opus 5.5", "medium")
    labels = [b["text"] for r in mirror.live_keyboard("", lang="en", session=SID)["keyboard"] for b in r]
    assert "🧠 Opus 5.5" in labels and any(x.startswith("🎚️ ") and x != "🎚️ Thinking" for x in labels)
    saved_models("Fable 5.2")                         # the status line saved a model but no level
    assert usage.session_model(SID) == ("Fable 5.2", "")   # another model's level is never borrowed
    saved_models("Opus 5.5")
    assert usage.session_model(SID) == ("Opus 5.5", "medium")
    assert usage.last_reply_model("") == ("", "") and usage.last_reply_model("/nonexistent/x.jsonl") == ("", "")


def test_a_later_effort_or_model_change_is_never_hidden_by_the_last_reply(transcript):
    """Review of PR #213: without a status line the level came from the last reply even after /effort."""
    transcript(dict(reply("claude-opus-5-5"), effort="medium"))
    assert usage.session_model(SID) == ("Opus 5.5", "medium")
    transcript(user_out("Set effort level to high (saved as your default for new sessions): Comprehensive"))
    assert usage.session_model(SID) == ("Opus 5.5", "high")
    transcript(command("fable"), user_out("Set model to `Fable 5.2`"))
    assert usage.session_model(SID) == ("Fable 5.2", "")      # the old model's level is not shown for the new one
    transcript(dict(reply("claude-fable-5-2"), effort="low"))
    assert usage.session_model(SID) == ("Fable 5.2", "low")


def test_the_transcript_is_read_once_per_change_and_from_its_end(transcript, monkeypatch):
    transcript(dict(reply("claude-opus-5-5"), effort="medium"))
    reads = []
    real = usage._tail_lines
    monkeypatch.setattr(usage, "_tail_lines", lambda path, size, span: reads.append(span) or real(path, size, span))
    usage._reply_models.clear()
    for _ in range(5):
        assert usage.last_reply_model(str(transcript.path)) == ("Opus 5.5", "medium")
    assert reads == [usage.REPLY_CHUNK]               # one small read, then the remembered answer
    transcript(dict(reply("claude-haiku-5-5"), effort="low"))
    assert usage.last_reply_model(str(transcript.path)) == ("Haiku 5.5", "low") and len(reads) == 2


# --- review of #215: a background agent's /effort row, and a transcript swapped in place ----------------

def test_a_background_agents_effort_answer_is_not_the_sessions():
    from ctb import usage
    reply = json.dumps({"type": "assistant", "effort": "medium", "message": {"model": "claude-opus-5-5"}})
    side = dict(user_out("Set effort level to high"), isSidechain=True)
    for row in (json.dumps(side), json.dumps(side, separators=(",", ":"))):  # with and without spaces
        assert usage._reply_model_in([reply, row]) == ("Opus 5.5", "medium")
    quoted = {"type": "user", "message": {"role": "user", "content": "it printed <local-command-stdout>Set effort level to high"}}
    assert usage._reply_model_in([reply, json.dumps(quoted)]) == ("Opus 5.5", "medium")  # only the answer itself
    assert usage._reply_model_in([reply, json.dumps(user_out("Set effort level to high"))]) == ("Opus 5.5", "high")


def test_a_swapped_transcript_is_read_again(tmp_path):
    import os
    from ctb import usage
    usage._reply_models.clear()
    row = lambda model: json.dumps({"type": "assistant", "message": {"model": model}}) + "\n"
    path, other = tmp_path / "t.jsonl", tmp_path / "o.jsonl"
    path.write_text(row("claude-opus-5-5"), encoding="utf-8")
    assert usage.last_reply_model(str(path))[0] == "Opus 5.5"
    st = path.stat()
    other.write_text(row("claude-opus-5-4"), encoding="utf-8")      # same size, another model
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))            # and the same saved change time
    os.replace(other, path)
    assert path.stat().st_size == st.st_size and path.stat().st_mtime_ns == st.st_mtime_ns
    assert usage.last_reply_model(str(path))[0] == "Opus 5.4"


def test_a_switch_to_another_model_is_not_confirmed(rig, monkeypatch):
    """Review of #254: only "Yes, switch to <the tapped model>" is pressed by the bridge."""
    rig.answer["dialog"] = "Switch model mid-conversation?\n❯ 1. Yes, switch to Haiku 5.5\n  2. No, go back\n\nEnter to confirm · Esc to cancel"
    cards = []
    monkeypatch.setattr(I.dialogs, "send_card", lambda *a, **k: cards.append(1) or True)
    monkeypatch.setattr(I, "MODEL_DIALOG_WAIT", 0)
    I.cli_command(cfg(), rig.bot, rig.m, SID, "/model claude-opus-5-5", menu=True)
    assert "1" not in rig.typed and cards == [1]


def test_a_reused_pane_gets_no_key(rig, monkeypatch):
    rig.answer["dialog"] = "Switch model mid-conversation?\n❯ 1. Yes, switch to Opus 5.5\n  2. No, go back\n\nEnter to confirm · Esc to cancel"
    cards = []
    monkeypatch.setattr(I.dialogs, "send_card", lambda *a, **k: cards.append(1) or True)
    monkeypatch.setattr(I, "MODEL_DIALOG_WAIT", 0)
    monkeypatch.setattr(I, "pane_runs_session", lambda pane, session, known_pid=None: False)
    I.cli_command(cfg(), rig.bot, rig.m, SID, "/model claude-opus-5-5", menu=True)
    assert "1" not in rig.typed and cards == [1]


def test_a_question_that_stays_open_after_the_key_goes_to_the_card(rig, monkeypatch):
    rig.answer["dialog"] = "Switch model mid-conversation?\n❯ 1. Yes, switch to Opus 5.5\n  2. No, go back\n\nEnter to confirm · Esc to cancel"
    monkeypatch.setattr(I, "pane_runs_session", lambda pane, session, known_pid=None: True)
    monkeypatch.setattr(I, "_pane_from_process", lambda session: "%5")
    cards = []
    monkeypatch.setattr(I.dialogs, "send_card", lambda *a, **k: cards.append(1) or True)
    monkeypatch.setattr(I, "MODEL_CONFIRM_SETTLE", 0)
    monkeypatch.setattr(I, "MODEL_DIALOG_WAIT", 0)
    I.cli_command(cfg(), rig.bot, rig.m, SID, "/model claude-opus-5-5", menu=True)
    assert "1" in rig.typed and cards == [1]


def test_another_pane_of_the_same_tmux_session_gets_no_key(rig, monkeypatch):
    """Review: pane_runs_session accepts any pane of the session's tmux window; the process record must name it."""
    rig.answer["dialog"] = "Switch model mid-conversation?\n❯ 1. Yes, switch to Opus 5.5\n  2. No, go back\n\nEnter to confirm · Esc to cancel"
    cards = []
    monkeypatch.setattr(I.dialogs, "send_card", lambda *a, **k: cards.append(1) or True)
    monkeypatch.setattr(I, "MODEL_DIALOG_WAIT", 0)
    monkeypatch.setattr(I, "pane_runs_session", lambda pane, session, known_pid=None: True)
    monkeypatch.setattr(I, "_pane_from_process", lambda session: "%9")
    I.cli_command(cfg(), rig.bot, rig.m, SID, "/model claude-opus-5-5", menu=True)
    assert "1" not in rig.typed and cards == [1]
