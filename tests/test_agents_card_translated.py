"""The agents card speaks the owner's language: each agent's task line is translated like its result, and a
result is never cut in the middle of a sentence."""
from __future__ import annotations

from test_agents_card import SID, T0, Bot, cfg, finish, start
from ctb import agents_card as AC, mirror as M


def test_task_line_is_replaced_by_its_translation():
    bot = Bot()
    start(bot, "a1", T0)
    finish(bot, "a1", T0 + 10, name="Find radio background noise")
    assert AC.agent_task_text(cfg(), SID, 11, "a1", "מציאת רעש רקע ברדיו", bot, now=T0 + 20, sleep=lambda s: None)
    text = (bot.methods("editMessageText") or bot.methods("sendMessage"))[-1]["text"]
    assert "מציאת רעש רקע ברדיו" in text and "Find radio background noise" not in text


def test_unknown_agent_or_empty_task_changes_nothing():
    bot = Bot()
    start(bot, "a1", T0)
    assert not AC.agent_task_text(cfg(), SID, 11, "zz", "משהו", bot, now=T0 + 5, sleep=lambda s: None)
    assert not AC.agent_task_text(cfg(), SID, 11, "a1", "", bot, now=T0 + 5, sleep=lambda s: None)


def test_long_result_ends_on_a_whole_sentence_or_an_ellipsis():
    long = ("I did not find a noise source in the audio chain. " * 2) + "The only change I made affects the soft bed only when on"
    out = AC._clean_summary(long)
    assert len(out) <= AC.SUMMARY_LIMIT
    assert out.endswith(".") or out.endswith("…")


def test_mirror_translates_the_task_and_edits_the_card(monkeypatch):
    seen = []
    monkeypatch.setattr(M, "_cached_translation", lambda text, c: "")
    monkeypatch.setattr(M, "translate_text", lambda text, c, fast=False: "תרגום של " + text)
    monkeypatch.setattr(M.agents_card, "agent_task_text", lambda c, s, t, a, task, b, source=None: seen.append((a, task, source)) or True)
    c = cfg(target_language="he", translate=True)
    M._translate_agent_task(c, SID, 11, "a1", "Setup via Telegram managed bots", Bot())
    assert seen == [("a1", "תרגום של Setup via Telegram managed bots", "Setup via Telegram managed bots")]


def test_mirror_leaves_the_task_when_translation_is_off_or_fails(monkeypatch):
    seen = []
    monkeypatch.setattr(M.agents_card, "agent_task_text", lambda *a, **k: seen.append(a) or True)
    M._translate_agent_task(cfg(target_language="he", translate=False), SID, 11, "a1", "Fix the parser", Bot())
    monkeypatch.setattr(M, "_cached_translation", lambda text, c: "")

    def boom(*a, **k):
        raise RuntimeError("engine down")
    monkeypatch.setattr(M, "translate_text", boom)
    M._translate_agent_task(cfg(target_language="he", translate=True), SID, 11, "a1", "Fix the parser", Bot())
    assert seen == []


def _task_on_card(bot):
    return (bot.methods("editMessageText") or bot.methods("sendMessage"))[-1]["text"]


def test_slow_translation_never_overwrites_a_later_runs_task():
    bot = Bot()
    AC.agent_started(cfg(), SID, 11, "a1", "worker", bot, now=T0, sleep=lambda s: None,
                     found={"id": "run1", "task": "Fix the parser"})
    finish(bot, "a1", T0 + 10, name="Fix the parser")
    AC.agent_started(cfg(), SID, 11, "a1", "worker", bot, now=T0 + 20, sleep=lambda s: None,
                     found={"id": "run2", "task": "Write the release notes"})
    # the first run's translation arrives late, after the agent resumed with a new task
    assert not AC.agent_task_text(cfg(), SID, 11, "a1", "תיקון המנתח", bot, now=T0 + 30, sleep=lambda s: None,
                                  source="Fix the parser")
    assert "Write the release notes" in _task_on_card(bot) and "תיקון המנתח" not in _task_on_card(bot)
    assert AC.agent_task_text(cfg(), SID, 11, "a1", "כתיבת הערות השחרור", bot, now=T0 + 40, sleep=lambda s: None,
                              source="Write the release notes")
    assert "כתיבת הערות השחרור" in _task_on_card(bot)


def test_finish_name_translation_keeps_a_verified_task():
    bot = Bot()
    AC.agent_started(cfg(), SID, 11, "a1", "worker", bot, now=T0, sleep=lambda s: None,
                     found={"id": "run1", "task": "Review authentication"})
    finish(bot, "a1", T0 + 10, name="worker")  # transcript had no description: the report name is the type
    assert not AC.agent_task_text(cfg(), SID, 11, "a1", "עובד", bot, now=T0 + 20, sleep=lambda s: None,
                                  source="worker")
    assert "Review authentication" in _task_on_card(bot)


def test_labelled_secrets_never_reach_the_summary_or_full_result():
    assert "hunter2" not in AC._clean_summary("Logged in with password: hunter2 and it worked.")
    bot = Bot()
    start(bot, "a1", T0)
    finish(bot, "a1", T0 + 10, summary="pw: hunter2", full="the admin password: hunter2 is set")
    st = AC._load(AC._path(SID))
    line = next(a for card in st["cards"].values() for a in card["agents"] if a.get("id") == "a1")
    assert "hunter2" not in line["summary"] and "hunter2" not in line["full"]


def test_strong_labels_hide_values_but_prose_and_exit_codes_stay():
    assert "hunter2" not in AC._clean_summary("api key = hunter2 was used")
    assert "s3cret9" not in AC._clean_summary("The password is s3cret9.")
    assert AC._clean_summary("The token is expired.") == "The token is expired."
    assert "exit code: 1234" in AC._clean_summary("Build failed, exit code: 1234")
