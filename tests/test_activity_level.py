"""The live-activity level: how much of an agent's work its topic shows while it works."""
import json

import pytest
from conftest import GOOD_TOKEN
from ctb import config as C, panel_settings, progress as P
from ctb.config import Config


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=False)


def rows(tmp_path, session="s1"):
    path = tmp_path / f"{session}.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch):
    monkeypatch.setattr("ctb.hooks.spawn", lambda *a, **k: None)


def step(tmp_path, output="one\ntwo\nthree\nfour\nfive\nsix"):
    P.append_progress("s1", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": output}, cfg(), root=tmp_path)


def test_the_level_is_normal_by_default_and_survives_a_damaged_file():
    assert C.activity_level() == "normal"
    C.write_private(C.state_dir() / C.ACTIVITY_LEVEL, "loud")
    assert C.activity_level() == "normal"
    C.set_activity_level("full")
    assert C.activity_level() == "full"
    C.set_activity_level("normal")
    assert not (C.state_dir() / C.ACTIVITY_LEVEL).exists()
    with pytest.raises(ValueError):
        C.set_activity_level("loud")


def test_quiet_keeps_the_steps_off_the_card(tmp_path):
    C.set_activity_level("quiet")
    step(tmp_path)
    assert [r for r in rows(tmp_path) if r.get("kind") == "action"] == []
    assert [r.get("kind") for r in rows(tmp_path)] == ["pulse"]  # the step still counts as a sign of life
    C.set_activity_level("normal")
    step(tmp_path)
    assert len([r for r in rows(tmp_path) if r.get("kind") == "action"]) == 1


def test_full_keeps_more_of_each_steps_output_and_more_steps_in_full(tmp_path):
    step(tmp_path)
    C.set_activity_level("full")
    step(tmp_path)
    normal, full = [r["output"] for r in rows(tmp_path) if r.get("kind") == "action"]
    assert normal == "one\ntwo…" and full == "one\ntwo\nthree\nfour\nfive…"
    cards = [f"<b>step {i}</b>\n<code>cmd {i}</code>" for i in range(8)]
    assert P.render_cards("en", cards, 0).count("<code>") == P.KEEP_FULL_VERBOSE
    C.set_activity_level("normal")
    assert P.render_cards("en", cards, 0).count("<code>") == P.KEEP_FULL


def test_the_cockpit_and_the_bot_menu_offer_the_level():
    from ctb import inbox, panel_page
    get, put, allowed = panel_settings.SETTINGS["activity"]
    assert allowed == C.ACTIVITY_LEVELS and get() == "normal"
    # the Cockpit saves through allowed() and write(): a text level must pass, a wrong one must not
    assert panel_settings.allowed("activity", "quiet") and not panel_settings.allowed("activity", "loud")
    assert not panel_settings.allowed("activity", 1) and not panel_settings.allowed("activity", True)
    panel_settings.write("activity", "quiet")
    assert panel_settings.read()["activity"] == "quiet"
    buttons = [b for row in inbox.global_markup(lang="en")["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "more:activity" and "Quiet" in b["text"] for b in buttons)
    assert 'setSetting("activity",v,d)' in panel_page.PAGE
    for lang in ("he", "en"):
        assert {"set_activity", "hint_activity", "act_quiet", "act_normal", "act_full"} <= set(panel_page.STRINGS[lang])


def _quiet_rig():
    from conftest import FakeBot
    from ctb.topics import TopicMap
    bot = FakeBot()
    TopicMap(C.state_dir() / "topics.json", bot, 123).ensure("s1", "T")
    bot.calls.clear()
    return bot, C.state_dir() / "progress"


def _emoji(bot):
    return [p for m, p in bot.calls if m == "sendMessage" and "\U0001F4BB" in p.get("text", "")]


def test_quiet_keeps_the_working_emoji_alive_without_showing_steps():
    """2026-11-11: at the quiet level a long turn looked stuck. The steps stay hidden, but the
    working emoji is shown and sent again now and then, so the topic shows the agent is alive."""
    bot, root = _quiet_rig()
    C.set_activity_level("quiet")
    step(root)
    P.run_send_progress(cfg(), "s1", bot=bot, throttle=0, now_fn=lambda: 1000.0)
    assert len(_emoji(bot)) >= 1
    assert all("ls" not in p.get("text", "") for m, p in bot.calls)  # no step card
    seen = len(_emoji(bot))
    step(root)
    P.run_send_progress(cfg(), "s1", bot=bot, throttle=0, now_fn=lambda: 1010.0)
    assert len(_emoji(bot)) == seen  # not on every step: at most once per PULSE_EVERY
    step(root)
    P.run_send_progress(cfg(), "s1", bot=bot, throttle=0, now_fn=lambda: 1000.0 + P.PULSE_EVERY + 1)
    assert len(_emoji(bot)) == seen + 1
    assert all("ls" not in p.get("text", "") for m, p in bot.calls)
