"""The owner may be on a phone: a move that stops at Claude's resume question carries a Resume button
(callback "rs:<session prefix>"), not an instruction to open the terminal. The tap re-reads the screen and
types only while the question is still there; only the owner can press it."""
from __future__ import annotations

import subprocess

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import accounts, inbox as I, usage
from ctb.config import Config
from ctb.i18n import t
from ctb.topics import TopicMap

SID = "abcd1234-0000-4000-8000-000000000001"
DIALOG = ("Resuming it will use about 8% of your 5-hour usage limit.\n"
          "{c1} 1. Resume\n{c2} 2. Start a new conversation\n")
ON_SCREEN = DIALOG.format(c1="❯", c2=" ")
GONE = "● Working on it\n\n❯ \n"


def cfg(lang="en"):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language=lang)


class Pane:
    """tmux stand-in: shows `screen`, records every key. Enter on the Resume row clears the dialog."""

    def __init__(self, screen, clears=True, name="tg-" + SID[:8]):
        self.screen, self.calls, self.clears, self.name = screen, [], clears, name

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "display":
            fmt = args[-1]
            return subprocess.CompletedProcess(args, 0, stdout=("123 " if "pane_pid" in fmt else "") + self.name + "\n")
        if self.clears and args[0] == "send-keys" and args[-1] == "Enter":
            self.screen = GONE
        out = self.screen if args[0] == "capture-pane" else ""
        return subprocess.CompletedProcess(args, 0, stdout=out)

    def keys(self):
        return [c[-1] for c in self.calls if c[0] == "send-keys"]


@pytest.fixture
def tap(tmp_path, bot, monkeypatch):
    topics = TopicMap(tmp_path / "topics", bot, 7)
    thread = topics.ensure(SID, "test")
    box = I.Inbox(tmp_path / "box")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "_terminal_background", lambda session, action: action() or True)  # run inline

    def press(sender=7, data="rs:" + SID[:8], chat_type="private"):
        m = {"chat": {"id": 7, "type": chat_type}, "message_thread_id": thread, "message_id": 5}
        q = {"id": "q1", "from": {"id": sender}, "message": m, "data": data}
        return I.handle_button(q, cfg(), bot, topics, box)

    return press


def toasts(bot):
    return [p.get("text") for p in bot.methods("answerCallbackQuery")]


def test_the_stuck_message_carries_the_resume_button_and_no_terminal_words(monkeypatch):
    accounts.add("small")
    accounts.record(SID, "main")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    monkeypatch.setattr(I, "_pane_command", lambda p: "zsh")
    monkeypatch.setattr(I, "_tmux", Pane(ON_SCREEN, clears=False))  # the move's own Enters change nothing
    sent = []
    say = lambda text, **extra: sent.append((text, extra))
    assert I.account_move(cfg(), FakeBot(), SID, "small", say) is False
    text, extra = sent[-1]
    assert extra["reply_markup"] == {"inline_keyboard": [[{"text": "▶️ Resume", "callback_data": "rs:" + SID[:8]}],
                                                         [{"text": "✖️ Not now", "callback_data": "more:dismiss"}]]}
    assert len(extra["reply_markup"]["inline_keyboard"][0][0]["callback_data"].encode()) <= 64
    assert "terminal" not in text.lower() and "mac" not in text.lower()


def test_the_limit_flow_message_carries_the_button_too(monkeypatch, bot):
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "account_move", lambda *a, **k: True)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    monkeypatch.setattr(I, "_tmux", Pane(ON_SCREEN, clears=False))
    assert I.auto_move(cfg(), bot, SID, 5, "small") is False
    sent = bot.methods("sendMessage")[-1]
    assert sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "rs:" + SID[:8]


def test_hebrew_ui_gets_a_hebrew_label_and_no_terminal_words():
    markup = I._resume_markup(SID, "he")
    assert markup["inline_keyboard"][0][0]["text"] == t("account_move_resume_btn", lang="he") != "▶️ Resume"
    text = t("account_move_stuck", lang="he", target="small")
    assert text.startswith("‏") and "טרמינל" not in text and "Mac" not in text


def test_a_tap_with_the_question_on_screen_chooses_resume(tap, monkeypatch, bot):
    pane = Pane(ON_SCREEN)
    monkeypatch.setattr(I, "_tmux", pane)
    picked = []
    real = I.answer_resume_dialog
    monkeypatch.setattr(I, "answer_resume_dialog", lambda target: picked.append(target) or real(target))
    assert tap() == "resume"
    assert picked == ["%1"]
    assert pane.keys() == ["Enter"]
    assert toasts(bot) == ["Resumed"]
    assert bot.methods("editMessageText")[-1]["text"] == t("account_move_resumed", lang="en")


def test_a_tap_when_the_question_is_gone_sends_no_keys(tap, monkeypatch, bot):
    pane = Pane(GONE)
    monkeypatch.setattr(I, "_tmux", pane)
    monkeypatch.setattr(I, "answer_resume_dialog", lambda target: pytest.fail("typed without the question"))
    assert tap() == "resume"
    assert pane.keys() == []
    assert toasts(bot) == ["The question is no longer on screen"]
    assert bot.methods("editMessageReplyMarkup")[-1]["reply_markup"] == {"inline_keyboard": []}


def test_a_quoted_question_in_history_sends_no_keys(tap, monkeypatch, bot):
    pane = Pane(ON_SCREEN + "\n".join(["line"] * 12) + "\n❯ \n")  # the question scrolled up: not at the bottom
    monkeypatch.setattr(I, "_tmux", pane)
    tap()
    assert pane.keys() == []


def test_a_tap_from_a_non_owner_is_refused(tap, monkeypatch, bot):
    pane = Pane(ON_SCREEN)
    monkeypatch.setattr(I, "_tmux", pane)
    assert tap(sender=8) == "ignored"
    assert pane.calls == []                                  # not even the screen was read
    assert toasts(bot) == ["Not available here"]


def test_a_button_from_another_session_is_refused(tap, monkeypatch, bot):
    pane = Pane(ON_SCREEN)
    monkeypatch.setattr(I, "_tmux", pane)
    assert tap(data="rs:deadbeef") == "ignored"
    assert pane.calls == []


def test_a_tap_that_cannot_answer_says_so_and_keeps_the_button(tap, monkeypatch, bot):
    monkeypatch.setattr(I, "_tmux", Pane(ON_SCREEN))
    monkeypatch.setattr(I, "answer_resume_dialog", lambda target: False)
    assert tap() == "resume"
    assert toasts(bot) == ["Could not answer it"]
    assert not bot.methods("editMessageText") and not bot.methods("editMessageReplyMarkup")


# --- review of PR #80 -------------------------------------------------------------------------------

PERMISSION = ("  Do you want to proceed?\n  ❯ 1. Yes\n    2. No\n")
QUOTED_OVER_PERMISSION = ("● Earlier the screen said:\n" + DIALOG.format(c1="❯", c2=" ") + "\n" + PERMISSION)


class Windows:
    """tmux stand-in with several windows: `screens` maps a target to its text, `names` to its session name."""

    def __init__(self, screens, names=None, clears=True):
        self.screens, self.names, self.clears, self.calls = dict(screens), dict(names or {}), clears, []

    def __call__(self, *args):
        self.calls.append(args)
        target = args[args.index("-t") + 1] if "-t" in args else ""
        if args[0] == "display":
            fmt = args[-1]
            name = self.names.get(target)
            if name is None:
                return subprocess.CompletedProcess(args, 1, stdout="")
            return subprocess.CompletedProcess(args, 0, stdout=("123 " if "pane_pid" in fmt else "") + name + "\n")
        if args[0] == "send-keys" and args[-1] == "Enter" and self.clears:
            self.screens[target] = GONE
        out = self.screens.get(target, "") if args[0] == "capture-pane" else ""
        return subprocess.CompletedProcess(args, 0, stdout=out)

    def keys(self):
        return [c[-1] for c in self.calls if c[0] == "send-keys"]


@pytest.mark.parametrize("screen", [QUOTED_OVER_PERMISSION,
                                    DIALOG.format(c1="❯", c2=" ") + "\n❯ \n",
                                    DIALOG.format(c1="❯", c2=" ") + "\n  Allow?  ❯ Yes   No\n"])
def test_a_quoted_question_above_another_dialog_or_the_prompt_is_not_the_question(screen):
    assert I.resume_dialog_shown(screen) is False


def test_the_real_question_with_hint_text_below_is_still_found():
    assert I.resume_dialog_shown(DIALOG.format(c1="❯", c2=" ") + "\n  Enter to confirm · Esc to cancel\n")


def test_a_tap_never_approves_a_permission_prompt_under_a_quoted_question(tap, monkeypatch, bot):
    pane = Pane(QUOTED_OVER_PERMISSION)
    monkeypatch.setattr(I, "_tmux", pane)
    tap()
    assert pane.keys() == []
    assert toasts(bot) == ["The question is no longer on screen"]


def test_pass_trust_never_approves_a_permission_prompt_under_a_quoted_question(monkeypatch):
    pane = Pane(QUOTED_OVER_PERMISSION)
    monkeypatch.setattr(I, "_tmux", pane)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    assert I.pass_trust("%1", "/tmp") is True
    assert pane.keys() == []


def test_the_new_window_branch_finds_the_question_although_the_saved_pane_is_stale(tap, monkeypatch, bot):
    name = "tg-" + SID[:8]
    win = Windows({"%9": "", "=" + name + ":": ON_SCREEN}, names={"=" + name + ":": name})  # %9 is dead
    monkeypatch.setattr(I, "_tmux", win)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%9")
    monkeypatch.setattr(I, "session_pid", lambda s: None)
    assert tap() == "resume"
    assert win.keys() == ["Enter"] and win.calls[-1][3] == "=" + name + ":"
    assert toasts(bot) == ["Resumed"]


def test_a_reused_pane_of_another_session_is_never_typed_into(tap, monkeypatch, bot):
    win = Windows({"%1": ON_SCREEN}, names={"%1": "tg-99999999"})  # %1 now belongs to someone else
    monkeypatch.setattr(I, "_tmux", win)
    monkeypatch.setattr(I, "session_pid", lambda s: None)
    tap()
    assert win.keys() == []
    # No window is provably this session's: the question may still be showing, so the button stays.
    assert toasts(bot) == ["Could not answer it"]
    assert bot.methods("editMessageReplyMarkup") == []


def test_the_loose_tg_prefix_target_is_never_used(tap, monkeypatch, bot):
    win = Windows({}, names={})
    monkeypatch.setattr(I, "_tmux", win)
    monkeypatch.setattr(I, "pane_for_session", lambda s: None)  # no saved pane at all
    tap()
    assert not any(c[c.index("-t") + 1] == "tg-" + SID[:8] for c in win.calls if "-t" in c)


def test_resumed_is_not_claimed_while_the_question_stays_on_screen(tap, monkeypatch, bot):
    pane = Pane(ON_SCREEN, clears=False)                        # Enter changes nothing
    monkeypatch.setattr(I, "_tmux", pane)
    assert tap() == "resume"
    assert toasts(bot) == ["Could not answer it"]
    assert not bot.methods("editMessageText") and not bot.methods("editMessageReplyMarkup")


def test_the_target_name_is_isolated_in_hebrew_and_plain_in_english(monkeypatch):
    for lang, wrapped in (("he", True), ("en", False)):
        sent = []
        monkeypatch.setattr(I.time, "sleep", lambda _: None)
        monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
        monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
        monkeypatch.setattr(I, "_pane_command", lambda p: "zsh")
        monkeypatch.setattr(I, "_tmux", Pane(ON_SCREEN, clears=False))
        accounts.add("small")
        accounts.record(SID, "main")
        I.account_move(cfg(lang), FakeBot(), SID, "small", lambda text, **extra: sent.append(text))
        assert ("\u2068small\u2069" in sent[-1]) is wrapped
